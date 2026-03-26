from dotenv import load_dotenv
load_dotenv()
from dynamov2.database.db_helper import db_helper
from dynamov2.database.models import (
    GitHubRepository,
    TrafficParameters,
    AgentRunResult,
    AgentTrafficParameters,
)
from sqlalchemy import or_, and_, func
import plotly.graph_objects as go

def generate_sankey_data():
    """Generate Sankey data for application labels.

    Returns a dict with counts and lists of repository ids for the
    requested queries.
    """
    session = db_helper.get_session()
    try:
        # Total count: repositories with cleaned_docker_compose_filepath not null
        total_query = (
            session.query(func.count())
            .select_from(GitHubRepository)
            .filter(GitHubRepository.cleaned_docker_compose_filepath.isnot(None))
        )
        total_with_cleaned = total_query.scalar() or 0

        # Total rows in TrafficParameters (processed repositories)
        processed_q = session.query(func.count()).select_from(TrafficParameters)
        processed_count = int(processed_q.scalar() or 0)

        # Working repositories: failure_reason is 'PCAP has not met the size requirements' OR is NULL
        working_q = session.query(func.count()).select_from(TrafficParameters).filter(
            or_(
                TrafficParameters.failure_reason == 'PCAP has not met the size requirements.',
                TrafficParameters.failure_reason.is_(None),
            )
        )
        working_count = int(working_q.scalar() or 0)

        # Repositories with >1Mb traffic: one_minute_check == True
        greater_q = session.query(func.count()).select_from(TrafficParameters).filter(
            TrafficParameters.one_minute_check.is_(True)
        )
        greater_count = int(greater_q.scalar() or 0)

        # Repositories with <1Mb traffic: failure_reason == 'PCAP has not met the size requirements'
        less_q = session.query(func.count()).select_from(TrafficParameters).filter(
            TrafficParameters.failure_reason == 'PCAP has not met the size requirements.'
        )
        less_count = int(less_q.scalar() or 0)

        # Repositories with errors
        errors = session.query(func.count()).select_from(TrafficParameters).filter(
            or_(
                TrafficParameters.failure_reason.like('%"status": "error"%'),
                TrafficParameters.failure_reason.like('%Image building failed%'),
                TrafficParameters.failure_reason.like('%service_errors%'),
                TrafficParameters.failure_reason.like('%failed to start containers%'),
                TrafficParameters.failure_reason.like('%Repo cannot be run with default docker-compose command. %'),
            )
        )
        errors_count = int(errors.scalar() or 0)

        # Repositories with missing docker compose filepaths (separate node)
        missing_compose_q = session.query(func.count()).select_from(TrafficParameters).filter(
            TrafficParameters.failure_reason == 'Repository has no valid docker compose filepaths.'
        )
        missing_compose_count = int(missing_compose_q.scalar() or 0)

        # Repositories that timed out (separate node)
        timeout_q = session.query(func.count()).select_from(TrafficParameters).filter(
            TrafficParameters.failure_reason.like('%timed out after 300 seconds%')
        )
        timeout_count = int(timeout_q.scalar() or 0)

        # Repositories that no longer exist (404) (separate node)
        missing_repo_q = session.query(func.count()).select_from(TrafficParameters).filter(
            TrafficParameters.failure_reason.like('%"status":"404"%')
        )
        missing_repo_count = int(missing_repo_q.scalar() or 0)

        # Repositories with file handling errors (separate node)
        handling_error_q = session.query(func.count()).select_from(TrafficParameters).filter(
            TrafficParameters.failure_reason.like('%No such file or directory%'),
            TrafficParameters.failure_reason.notlike('%status": "error%')
        )
        handling_error_count = int(handling_error_q.scalar() or 0)

        known_outgoing = (
            working_count
            + errors_count
            + missing_compose_count
            + timeout_count
            + missing_repo_count
            + handling_error_count
        )
        other_errors_count = processed_count - known_outgoing
        all_errors_count = (
            missing_compose_count
            + timeout_count
            + missing_repo_count
            + handling_error_count
            + other_errors_count
        )

        # Repositories where LLM found application traffic present
        application_q = session.query(func.count()).select_from(TrafficParameters).filter(
            TrafficParameters.application_traffic_present == 'true'
        )
        application_count = int(application_q.scalar() or 0)

        result = {
            "total_with_cleaned": int(total_with_cleaned),
            "processed_count": processed_count,
            "working_count": working_count,
            "greater_1mb_count": greater_count,
            "less_1mb_count": less_count,
            "repositories_with_errors_count": errors_count,
            "repositories_missing_compose_count": missing_compose_count,
            "repositories_timeout_count": timeout_count,
            "repositories_missing_repo_count": missing_repo_count,
            "repositories_handling_error_count": handling_error_count,
            "repositories_other_errors_count": other_errors_count,
            "repositories_all_errors_count": all_errors_count,
            "repositories_with_application_count": application_count,
        }

        agent_stats = generate_agent_fixing_stats()
        result.update(agent_stats)
        return result
    finally:
        session.close()


def generate_agent_fixing_stats():
    """Return counts for agent run fixing results and post-fix traffic/app presence.

    Notes:
      - AgentRunResult and AgentTrafficParameters are keyed by (repository_id/id, model, run_id).
      - For Sankey conservation, we count "fixed" repos only when a matching
        AgentTrafficParameters row exists for the same (repo, model, run_id).

    Returns a dict with keys:
      - docker_errors_fixed: count distinct repository ids where codex_working is True AND
        matching agent_traffic_parameters exists
      - docker_errors_not_fixed: count distinct repository ids where codex_working is False
      - agent_fixed_greater_1mb_count: fixed repos where agent one_minute_check is True
      - agent_fixed_less_1mb_count: fixed repos where agent one_minute_check is False
      - repository_with_application_after_fixing: fixed repos with >1Mb where
        application_traffic_present == 'true'
    """
    session = db_helper.get_session()
    try:
        join_on = and_(
            AgentTrafficParameters.id == AgentRunResult.repository_id,
            AgentTrafficParameters.model == AgentRunResult.model,
            AgentTrafficParameters.run_id == AgentRunResult.run_id,
        )
        docker_errors_fixed = int(
            session.query(func.count(func.distinct(AgentRunResult.repository_id)))
            .select_from(AgentRunResult)
            .join(AgentTrafficParameters, join_on)
            .filter(AgentRunResult.codex_working.is_(True))
            .scalar()
            or 0
        )

        not_fixed_q = (
            session.query(func.count(func.distinct(AgentRunResult.repository_id)))
            .filter(AgentRunResult.codex_working.is_(False))
        )
        docker_errors_not_fixed = int(not_fixed_q.scalar() or 0)

        agent_fixed_greater_1mb_count = int(
            session.query(func.count(func.distinct(AgentRunResult.repository_id)))
            .select_from(AgentRunResult)
            .join(AgentTrafficParameters, join_on)
            .filter(
                AgentRunResult.codex_working.is_(True),
                AgentTrafficParameters.one_minute_check.is_(True),
            )
            .scalar()
            or 0
        )

        agent_fixed_less_1mb_count = int(
            session.query(func.count(func.distinct(AgentRunResult.repository_id)))
            .select_from(AgentRunResult)
            .join(AgentTrafficParameters, join_on)
            .filter(
                AgentRunResult.codex_working.is_(True),
                or_(
                    AgentTrafficParameters.one_minute_check.is_(False),
                    AgentTrafficParameters.one_minute_check.is_(None),
                ),
            )
            .scalar()
            or 0
        )

        repository_with_application_after_fixing = int(
            session.query(func.count(func.distinct(AgentRunResult.repository_id)))
            .select_from(AgentRunResult)
            .join(AgentTrafficParameters, join_on)
            .filter(
                AgentRunResult.codex_working.is_(True),
                AgentTrafficParameters.one_minute_check.is_(True),
                func.lower(AgentTrafficParameters.application_traffic_present) == "true",
            )
            .scalar()
            or 0
        )

        return {
            "docker_errors_fixed": docker_errors_fixed,
            "docker_errors_not_fixed": docker_errors_not_fixed,
            "agent_fixed_greater_1mb_count": agent_fixed_greater_1mb_count,
            "agent_fixed_less_1mb_count": agent_fixed_less_1mb_count,
            "repository_with_application_after_fixing": repository_with_application_after_fixing,
        }
    finally:
        session.close()



if __name__ == "__main__":
    sankey_data = generate_sankey_data()
    print(sankey_data)
    # Node labels in a stable order
    label_keys = list(sankey_data.keys())
    # Friendly labels and node colors
    friendly = {
        "total_with_cleaned": "Repositories",
        "processed_count": "Processed",
        "working_count": "Working repositories",
        "greater_1mb_count": ">1Mb traffic",
        "less_1mb_count": "<1Mb traffic",
        "repositories_with_errors_count": "Repositories with errors",
        "repositories_missing_compose_count": "Missing docker compose filepaths",
        "repositories_timeout_count": "Timeout",
        "repositories_missing_repo_count": "Repository does not exist",
        "repositories_handling_error_count": "Handling error",
        "repositories_other_errors_count": "Other errors",
        "repositories_all_errors_count": "Other form of errors",
        "repositories_with_application_count": "Repositories with Application traffic",
        "docker_errors_fixed": "Errors fixed after agent",
        "docker_errors_not_fixed": "Errors not fixed after agent",
        "agent_fixed_greater_1mb_count": ">1Mb traffic after fixing",
        "agent_fixed_less_1mb_count": "<1Mb traffic after fixing",
        "repository_with_application_after_fixing": "Repositories with applications after fixing",
    }
    color_map = {
        "total_with_cleaned": "#4B8BBE",
        "processed_count": "#4B8BBE",
        "working_count": "#2ca02c",
        "greater_1mb_count": "#1f77b4",
        "less_1mb_count": "#ff7f0e",
        "repositories_with_errors_count": "#d62728",
        "repositories_missing_compose_count": "#bcbd22",
        "repositories_timeout_count": "#17becf",
        "repositories_missing_repo_count": "#c7c7c7",
        "repositories_handling_error_count": "#aec7e8",
        "repositories_other_errors_count": "#c49c94",
        "repositories_all_errors_count": "#ff9896",
        "repositories_with_application_count": "#9467bd",
        "docker_errors_fixed": "#8c564b",
        "docker_errors_not_fixed": "#e377c2",
        "agent_fixed_greater_1mb_count": "#1f77b4",
        "agent_fixed_less_1mb_count": "#ff7f0e",
        "repository_with_application_after_fixing": "#7f7f7f",
    }

    def build_sankey(
        links_subset,
        stages_subset,
        stage_order_subset,
        output_path,
        title_text,
        width,
        height,
        y_span_by_stage=None,
        min_link_value=0.0,
    ):
        # Determine which keys are needed for this subset
        needed = set()
        for s, t in links_subset:
            needed.add(s)
            needed.add(t)

        # Keep a stable order using the original label_keys
        keys_subset = [k for k in label_keys if k in needed]
        if not keys_subset:
            print(f"No keys available for {title_text}, skipping")
            return None

        labels_sub = [f"{friendly.get(k, k)}\n({int(sankey_data.get(k,0) or 0)})" for k in keys_subset]
        colors_sub = [color_map.get(k, "#888888") for k in keys_subset]

        index_sub = {k: i for i, k in enumerate(keys_subset)}
        source_sub = [index_sub[s] for s, t in links_subset if s in index_sub and t in index_sub]
        target_sub = [index_sub[t] for s, t in links_subset if s in index_sub and t in index_sub]
        value_sub = []
        for s, t in links_subset:
            if s in index_sub and t in index_sub:
                val = int(sankey_data.get(t, 0) or 0)
                if min_link_value and val == 0:
                    val = float(min_link_value)
                value_sub.append(val)

        def hex_to_rgba_local(h, a=0.45):
            h = h.lstrip("#")
            if len(h) == 3:
                h = ''.join(c*2 for c in h)
            r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
            return f"rgba({r},{g},{b},{a})"

        link_colors_sub = [hex_to_rgba_local(colors_sub[s]) for s in source_sub]

        # Compute positions using the provided stage mapping
        nodes_by_stage_sub = {}
        for k in keys_subset:
            s = stages_subset.get(k, 0)
            nodes_by_stage_sub.setdefault(s, []).append(k)

        for s, keys in nodes_by_stage_sub.items():
            order = stage_order_subset.get(s)
            if order:
                order_index = {k: i for i, k in enumerate(order)}
                keys.sort(key=lambda k: order_index.get(k, len(order)))

        min_stage_sub = min(nodes_by_stage_sub.keys()) if nodes_by_stage_sub else 0
        max_stage_sub = max(nodes_by_stage_sub.keys()) if nodes_by_stage_sub else 1
        den = max_stage_sub - min_stage_sub
        node_x_sub = [0.0] * len(keys_subset)
        node_y_sub = [0.0] * len(keys_subset)
        for s, keys in nodes_by_stage_sub.items():
            n = len(keys)
            for i, k in enumerate(keys):
                idx = keys_subset.index(k)
                # Normalize x to the subset's stage range so leftmost stage becomes 0.0
                if den > 0:
                    x = (s - min_stage_sub) / float(den)
                else:
                    x = 0.5
                if n == 1:
                    y = 0.5
                else:
                    if y_span_by_stage and s in y_span_by_stage:
                        y_min, y_max = y_span_by_stage[s]
                    elif n >= 6:
                        y_min, y_max = 0.02, 0.98
                    else:
                        y_min, y_max = 0.08, 0.92
                    y = y_min + (i / float(n - 1)) * (y_max - y_min)
                node_x_sub[idx] = x
                node_y_sub[idx] = y

        # Small per-node nudges to avoid label/link overlap (values in normalized 0..1 coords)
        nudges = {
            "repository_with_application_after_fixing": (0.25, -0.1),
            "repositories_with_application_count": (0.0, 0.1),
            # add or tune more entries as needed
        }
        for k, (dx, dy) in nudges.items():
            if k in keys_subset:
                idx = keys_subset.index(k)
                node_x_sub[idx] = min(max(node_x_sub[idx] + dx, 0.0), 1.0)
                node_y_sub[idx] = min(max(node_y_sub[idx] + dy, 0.0), 1.0)

        fig_sub = go.Figure(data=[go.Sankey(
            arrangement="fixed",
            node=dict(
                pad=30,
                thickness=15,
                line=dict(color="black", width=0.5),
                label=labels_sub,
                color=colors_sub,
                x=node_x_sub,
                y=node_y_sub,
            ),
            link=dict(
                source=source_sub,
                target=target_sub,
                value=value_sub,
                color=link_colors_sub,
            ),
        )])
        fig_sub.update_layout(
            width=width,
            height=height,
            margin=dict(l=50, r=50, t=80, b=50),
            title={"text": title_text, "font": {"size": 20}},
            font={"size": 18, "family": "Arial"},
        )
        fig_sub.write_html(output_path)
        print(f"Saved HTML to {output_path}")
        return fig_sub

    main_links = [
        ("total_with_cleaned", "processed_count"),
        ("processed_count", "working_count"),
        ("processed_count", "repositories_with_errors_count"),
        ("processed_count", "repositories_all_errors_count"),
        ("working_count", "greater_1mb_count"),
        ("working_count", "less_1mb_count"),
        ("greater_1mb_count", "repositories_with_application_count"),
        ("repositories_with_errors_count", "docker_errors_fixed"),
        ("repositories_with_errors_count", "docker_errors_not_fixed"),
        ("docker_errors_fixed", "agent_fixed_greater_1mb_count"),
        ("docker_errors_fixed", "agent_fixed_less_1mb_count"),
        ("agent_fixed_greater_1mb_count", "repository_with_application_after_fixing"),
    ]
    main_stages = {
        "total_with_cleaned": 0,
        "processed_count": 1,
        "working_count": 2,
        "repositories_all_errors_count": 2,
        "repositories_with_errors_count": 2,
        "greater_1mb_count": 3,
        "less_1mb_count": 3,
        "docker_errors_fixed": 3,
        "docker_errors_not_fixed": 3,
        "repositories_with_application_count": 4,
        "agent_fixed_greater_1mb_count": 4,
        "agent_fixed_less_1mb_count": 4,
        "repository_with_application_after_fixing": 5,
    }
    main_stage_order = {
        2: [
            "working_count",
            "repositories_all_errors_count",
            "repositories_with_errors_count",
        ],
        3: [
            "greater_1mb_count",
            "less_1mb_count",
            "docker_errors_fixed",
            "docker_errors_not_fixed",
        ],
        4: [
            "repositories_with_application_count",
            "agent_fixed_greater_1mb_count",
            "agent_fixed_less_1mb_count",
        ],
        5: [
            "repository_with_application_after_fixing",
        ],
    }
    fig_main = build_sankey(
        main_links,
        main_stages,
        main_stage_order,
        "sankey_chart.html",
        "Sankey Funnel Chart",
        1600,
        900,
        y_span_by_stage={2: (0.08, 0.92), 3: (0.1, 0.9)},
        min_link_value=0.001,
    )

    working_links = [
        ("working_count", "greater_1mb_count"),
        ("working_count", "less_1mb_count"),
        ("greater_1mb_count", "repositories_with_application_count"),
    ]
    working_stages = {
        "working_count": 0,
        "greater_1mb_count": 1,
        "less_1mb_count": 1,
        "repositories_with_application_count": 2,
    }
    working_stage_order = {
        1: ["greater_1mb_count", "less_1mb_count"],
    }
    fig_working = build_sankey(
        working_links,
        working_stages,
        working_stage_order,
        "sankey_working.html",
        "Sankey - Working Repositories",
        1400,
        700,
        y_span_by_stage={1: (0.2, 0.8), 2: (0.1, 0.9)},
    )

    error_links = [
        ("repositories_with_errors_count", "docker_errors_fixed"),
        ("repositories_with_errors_count", "docker_errors_not_fixed"),
        ("docker_errors_fixed", "agent_fixed_greater_1mb_count"),
        ("docker_errors_fixed", "agent_fixed_less_1mb_count"),
        ("agent_fixed_greater_1mb_count", "repository_with_application_after_fixing"),
    ]
    error_stages = {
        "repositories_with_errors_count": 0,
        "docker_errors_fixed": 1,
        "docker_errors_not_fixed": 1,
        "agent_fixed_greater_1mb_count": 2,
        "agent_fixed_less_1mb_count": 2,
        "repository_with_application_after_fixing": 3,
    }
    error_stage_order = {
        1: ["docker_errors_fixed", "docker_errors_not_fixed"],
        2: ["agent_fixed_greater_1mb_count", "agent_fixed_less_1mb_count"],
    }
    fig_errors = build_sankey(
        error_links,
        error_stages,
        error_stage_order,
        "sankey_errors.html",
        "Sankey - Error Repositories",
        1400,
        800,
        y_span_by_stage={1: (0.08, 0.92), 2: (0.15, 0.85), 3: (0.2, 0.8)},
        min_link_value=0.001,
    )

    if fig_main is not None:
        fig_main.show()
    if fig_working is not None:
        fig_working.show()
    if fig_errors is not None:
        fig_errors.show()