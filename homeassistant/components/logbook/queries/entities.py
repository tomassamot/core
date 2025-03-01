"""Entities queries for logbook."""
from __future__ import annotations

from collections.abc import Iterable

import sqlalchemy
from sqlalchemy import lambda_stmt, select, union_all
from sqlalchemy.sql.elements import ColumnElement
from sqlalchemy.sql.lambdas import StatementLambdaElement
from sqlalchemy.sql.selectable import CTE, CompoundSelect, Select

from homeassistant.components.recorder.db_schema import (
    ENTITY_ID_IN_EVENT,
    ENTITY_ID_LAST_UPDATED_INDEX_TS,
    OLD_ENTITY_ID_IN_EVENT,
    EventData,
    Events,
    States,
)

from .common import (
    apply_events_context_hints,
    apply_states_context_hints,
    apply_states_filters,
    select_events_context_id_subquery,
    select_events_context_only,
    select_events_without_states,
    select_states,
    select_states_context_only,
)


def _select_entities_context_ids_sub_query(
    start_day: float,
    end_day: float,
    event_types: tuple[str, ...],
    entity_ids: list[str],
    json_quoted_entity_ids: list[str],
) -> Select:
    """Generate a subquery to find context ids for multiple entities."""
    union = union_all(
        select_events_context_id_subquery(start_day, end_day, event_types).where(
            apply_event_entity_id_matchers(json_quoted_entity_ids)
        ),
        apply_entities_hints(select(States.context_id_bin))
        .filter(
            (States.last_updated_ts > start_day) & (States.last_updated_ts < end_day)
        )
        .where(States.entity_id.in_(entity_ids)),
    )
    return select(union.c.context_id_bin).group_by(union.c.context_id_bin)


def _apply_entities_context_union(
    sel: Select,
    start_day: float,
    end_day: float,
    event_types: tuple[str, ...],
    entity_ids: list[str],
    json_quoted_entity_ids: list[str],
) -> CompoundSelect:
    """Generate a CTE to find the entity and device context ids and a query to find linked row."""
    entities_cte: CTE = _select_entities_context_ids_sub_query(
        start_day,
        end_day,
        event_types,
        entity_ids,
        json_quoted_entity_ids,
    ).cte()
    # We used to optimize this to exclude rows we already in the union with
    # a States.entity_id.not_in(entity_ids) but that made the
    # query much slower on MySQL, and since we already filter them away
    # in the python code anyways since they will have context_only
    # set on them the impact is minimal.
    return sel.union_all(
        states_select_for_entity_ids(start_day, end_day, entity_ids),
        apply_events_context_hints(
            select_events_context_only()
            .select_from(entities_cte)
            .outerjoin(Events, entities_cte.c.context_id_bin == Events.context_id_bin)
        ).outerjoin(EventData, (Events.data_id == EventData.data_id)),
        apply_states_context_hints(
            select_states_context_only()
            .select_from(entities_cte)
            .outerjoin(States, entities_cte.c.context_id_bin == States.context_id_bin)
        ),
    )


def entities_stmt(
    start_day: float,
    end_day: float,
    event_types: tuple[str, ...],
    entity_ids: list[str],
    json_quoted_entity_ids: list[str],
) -> StatementLambdaElement:
    """Generate a logbook query for multiple entities."""
    return lambda_stmt(
        lambda: _apply_entities_context_union(
            select_events_without_states(start_day, end_day, event_types).where(
                apply_event_entity_id_matchers(json_quoted_entity_ids)
            ),
            start_day,
            end_day,
            event_types,
            entity_ids,
            json_quoted_entity_ids,
        ).order_by(Events.time_fired_ts)
    )


def states_select_for_entity_ids(
    start_day: float, end_day: float, entity_ids: list[str]
) -> Select:
    """Generate a select for states from the States table for specific entities."""
    return apply_states_filters(
        apply_entities_hints(select_states()), start_day, end_day
    ).where(States.entity_id.in_(entity_ids))


def apply_event_entity_id_matchers(
    json_quoted_entity_ids: Iterable[str],
) -> ColumnElement[bool]:
    """Create matchers for the entity_id in the event_data."""
    return sqlalchemy.or_(
        ENTITY_ID_IN_EVENT.is_not(None)
        & sqlalchemy.cast(ENTITY_ID_IN_EVENT, sqlalchemy.Text()).in_(
            json_quoted_entity_ids
        ),
        OLD_ENTITY_ID_IN_EVENT.is_not(None)
        & sqlalchemy.cast(OLD_ENTITY_ID_IN_EVENT, sqlalchemy.Text()).in_(
            json_quoted_entity_ids
        ),
    )


def apply_entities_hints(sel: Select) -> Select:
    """Force mysql to use the right index on large selects."""
    return sel.with_hint(
        States, f"FORCE INDEX ({ENTITY_ID_LAST_UPDATED_INDEX_TS})", dialect_name="mysql"
    ).with_hint(
        States,
        f"FORCE INDEX ({ENTITY_ID_LAST_UPDATED_INDEX_TS})",
        dialect_name="mariadb",
    )
