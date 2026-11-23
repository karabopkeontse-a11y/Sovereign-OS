"""Workspace-scoped multi-agent collaboration for SovereignOS."""
from __future__ import annotations

from .specialists import specialist_for, specialist_prompt


def _get(item, key, default=None):
    if isinstance(item, dict):
        return item.get(key, default)
    try:
        return getattr(item, key, default)
    except (TypeError, AttributeError):
        return default


def _content(response):
    value = getattr(response, "content", response)
    return value if isinstance(value, str) else str(value)


def run_collaboration(
    store,
    router,
    workspace,
    task: str,
    conversation_id: str | None = None,
    memory_context: str = "",
    max_agents: int = 4,
    specialist_ids: list[str] | None = None,
) -> dict:
    """Run proposals, peer review and leader synthesis inside one workspace."""
    from langchain_core.messages import HumanMessage, SystemMessage

    task = (task or "").strip()
    if not task:
        raise ValueError("A task description is required.")

    workspace_id = _get(workspace, "id")
    workspace_name = _get(workspace, "name", "SovereignOS")
    workspace_purpose = _get(workspace, "purpose", "")
    if not workspace_id:
        raise ValueError("A valid workspace is required.")

    candidates = getattr(router, "candidates", [])
    if not candidates:
        raise RuntimeError("No configured model candidates are available.")

    existing = store.list_agent_identities(workspace_id)

    def find_identity(role, name):
        return next(
            (item for item in existing
             if _get(item, "role") == role and _get(item, "name") == name),
            None,
        )

    def ensure_identity(role, name, mission, info):
        item = find_identity(role, name)
        if item:
            return item
        item = store.create_agent_identity(
            workspace_id=workspace_id,
            role=role,
            name=name,
            mission=mission,
            provider=info.provider,
            model=info.model,
            runtime=getattr(info, "runtime", ""),
        )
        existing.append(item)
        return item

    roster = []
    seen = set()
    for index, candidate in enumerate(candidates):
        specialist = specialist_for(index, candidate)
        key = (specialist["role"], specialist["name"])
        if key in seen:
            continue
        seen.add(key)
        roster.append((specialist, candidate))

    if specialist_ids is not None:
        requested = list(dict.fromkeys(specialist_ids))
        if len(requested) < 2:
            raise ValueError("Select at least two Specialists.")
        if len(requested) > max_agents:
            raise ValueError("Selected Specialists exceed the agent limit.")
        available = {item[0]["id"] for item in roster}
        unknown = set(requested) - available
        if unknown:
            raise ValueError(
                "Unknown specialist selection: " + ", ".join(sorted(unknown))
            )
        roster = [
            item for item in roster if item[0]["id"] in requested
        ]

    roster = roster[:max(1, min(int(max_agents), 4))]
    if not roster:
        raise RuntimeError("No worker agents could be assigned.")

    leader_info = candidates[0].info
    leader = ensure_identity(
        "leader", "Sovereign Lead",
        "Coordinate agents, resolve disagreements and verify the final report.",
        leader_info,
    )
    leader_id = _get(leader, "id")
    if not leader_id:
        raise RuntimeError("Could not establish the workspace leader identity.")

    specialists = []
    for specialist, candidate in roster:
        identity = ensure_identity(
            specialist["role"], specialist["name"],
            specialist["mission"], candidate.info,
        )
        specialists.append((specialist, identity))

    discussion = store.create_agent_discussion(
        workspace_id=workspace_id,
        title=task[:240],
        leader_agent_id=leader_id,
        conversation_id=conversation_id,
    )
    discussion_id = _get(discussion, "id")
    if not discussion_id:
        raise RuntimeError("Could not create the workspace discussion.")

    assignments = {}
    for specialist, identity in specialists:
        agent_id = _get(identity, "id")
        assignment = store.create_agent_assignment(
            workspace_id,
            discussion_id,
            agent_id,
            f"Provide an independent {specialist['role']} analysis, then "
            "review colleagues' proposals and contribute evidence, risks "
            "and recommendations to the consolidated solution.",
        )
        assignments[agent_id] = _get(assignment, "id")
        if not assignments[agent_id]:
            raise RuntimeError(f"Could not create assignment for {specialist['name']}.")

    leader_assignment = store.create_agent_assignment(
        workspace_id,
        discussion_id,
        leader_id,
        "Review all agent proposals and peer reviews, resolve or disclose "
        "disagreements, verify the response, and produce the consolidated report.",
    )
    leader_assignment_id = _get(leader_assignment, "id")
    if not leader_assignment_id:
        raise RuntimeError("Could not create the Leader assignment.")

    def set_assignment(agent_id, status, result=""):
        store.update_agent_assignment(
            workspace_id, assignments[agent_id], status, result
        )

    def record(speaker_type, content, agent_id=None):
        store.append_agent_message(
            workspace_id, discussion_id, speaker_type, str(content), agent_id
        )

    record("user", task)
    context = (
        f"Workspace: {workspace_name}\n"
        f"Workspace purpose: {workspace_purpose}\n"
        f"Relevant workspace memories:\n{memory_context or '(none)'}\n"
        f"User task:\n{task}"
    )

    proposals = []
    for specialist, identity in specialists:
        agent_id = _get(identity, "id")
        system = (
            specialist_prompt(specialist, workspace_name)
            + "\nYou are participating in a real multi-agent discussion. "
              "Give an independent proposal. State assumptions, evidence, "
              "risks and questions. Do not claim actions were executed."
        )
        set_assignment(agent_id, "in_progress")
        try:
            response, info, used_index = router.invoke(
                [SystemMessage(content=system), HumanMessage(content=context)],
                start_index=specialist["index"],
            )
            reply = _content(response)
        except Exception as exc:
            set_assignment(agent_id, "failed", f"Proposal failed: {exc}")
            raise
        entry = {
            "agent_id": agent_id,
            "name": specialist["name"],
            "role": specialist["role"],
            "provider": info.provider,
            "model": info.model,
            "reply": reply,
        }
        proposals.append(entry)
        record("agent", f"[{entry['name']} - proposal]\n{reply}", agent_id)

    reviews = []
    for specialist, identity in specialists:
        agent_id = _get(identity, "id")
        peers = "\n\n".join(
            f"{p['name']} ({p['role']}):\n{p['reply']}"
            for p in proposals if p["agent_id"] != agent_id
        ) or "(No other agents were assigned.)"

        system = (
            specialist_prompt(specialist, workspace_name)
            + "\nYou are now reviewing your colleagues' proposals. "
              "Address their ideas directly. Identify errors, missing "
              "evidence, risks and useful disagreements. Ask questions "
              "where necessary. Improve the combined solution. Do not "
              "simply repeat your first answer."
        )
        try:
            response, info, used_index = router.invoke(
                [
                    SystemMessage(content=system),
                    HumanMessage(content=context + "\n\nColleagues' proposals:\n" + peers),
                ],
                start_index=specialist["index"],
            )
            reply = _content(response)
        except Exception as exc:
            set_assignment(agent_id, "failed", f"Peer review failed: {exc}")
            raise
        entry = {
            "agent_id": agent_id,
            "name": specialist["name"],
            "provider": info.provider,
            "model": info.model,
            "reply": reply,
        }
        reviews.append(entry)
        record("agent", f"[{entry['name']} - peer review]\n{reply}", agent_id)

    # Each specialist responds to a specific colleague's review.
    agent_responses = []

    for index, (specialist, identity) in enumerate(specialists):
        agent_id = _get(identity, "id")
        reviewer_specialist, reviewer_identity = specialists[
            (index + 1) % len(specialists)
        ]
        reviewer_id = _get(reviewer_identity, "id")

        reviewer_entry = next(
            item for item in reviews if item["agent_id"] == reviewer_id
        )

        # Read the persisted discussion, scoped to this workspace.
        history = store.list_agent_messages(workspace_id, discussion_id)
        history_text = "\n\n".join(
            f"{message['speaker_type']} "
            f"({message.get('agent_id') or 'user'}):\n{message['content']}"
            for message in history
        )

        system = (
            specialist_prompt(specialist, workspace_name)
            + f"\nYou are responding directly to {reviewer_specialist['name']}."
            + "\nRead their peer review carefully. Identify the points that "
              "specifically concern your proposal. Answer those points "
              "directly, explain where you agree or disagree, correct any "
              "weaknesses, and state what evidence would change your view. "
              "Do not invent evidence or claim actions were executed. "
              "Your response must engage with the actual review, not merely "
              "repeat your original proposal."
        )

        prompt = (
            context
            + "\n\nPersisted discussion so far:\n"
            + history_text
            + f"\n\nYour colleague {reviewer_specialist['name']} "
              "provided this peer review:\n"
            + reviewer_entry["reply"]
            + "\n\nRespond specifically to that colleague."
        )

        try:
            response, info, used_index = router.invoke(
                [SystemMessage(content=system), HumanMessage(content=prompt)],
                start_index=specialist["index"],
            )
            reply = _content(response)
        except Exception as exc:
            set_assignment(
                agent_id, "failed", f"Discussion response failed: {exc}"
            )
            raise

        entry = {
            "agent_id": agent_id,
            "name": specialist["name"],
            "role": specialist["role"],
            "responding_to": reviewer_id,
            "reviewer_name": reviewer_specialist["name"],
            "provider": info.provider,
            "model": info.model,
            "reply": reply,
        }
        agent_responses.append(entry)
        record(
            "agent",
            f"[{entry['name']} - response to "
            f"{entry['reviewer_name']}]\n{reply}",
            agent_id,
        )

    team_record = "\n\n".join(
        f"{p['name']} proposal:\n{p['reply']}\n"
        f"Peer review:\n"
        f"{next(r['reply'] for r in reviews if r['agent_id'] == p['agent_id'])}\n"
        f"Response:\n"
        f"{next(r['reply'] for r in agent_responses if r['agent_id'] == p['agent_id'])}"
        for p in proposals
    )

    leader_system = (
        f"You are the SovereignOS Leader for the {workspace_name} workspace. "
        "You are accountable for the team's consolidated report. Compare the "
        "agents' proposals, peer reviews and direct responses. Moderate the "
        "exchange: identify which criticisms were answered, which were not, "
        "and whether any positions changed. Resolve disagreements where "
        "evidence permits; otherwise disclose them. Check that the response "
        "addresses the user's task, separates facts from assumptions, identifies "
        "unverified claims and lists any required human approvals. Never claim "
        "that tools were run or external actions completed unless evidence proves it."
    )
    store.update_agent_assignment(
        workspace_id, leader_assignment_id, "in_progress"
    )
    try:
        final_response, final_info, final_index = router.invoke(
            [
                SystemMessage(content=leader_system),
                HumanMessage(content=context + "\n\nAgent discussion:\n" + team_record),
            ],
            start_index=0,
        )
        report = _content(final_response)
    except Exception as exc:
        store.update_agent_assignment(
            workspace_id, leader_assignment_id, "failed",
            f"Leader synthesis failed: {exc}",
        )
        raise

    record("leader", report, leader_id)

    for proposal in proposals:
        agent_id = proposal["agent_id"]
        review = next(
            item["reply"] for item in reviews
            if item["agent_id"] == agent_id
        )
        response = next(
            item["reply"] for item in agent_responses
            if item["agent_id"] == agent_id
        )
        set_assignment(
            agent_id,
            "completed",
            f"Proposal:\n{proposal['reply']}\n\n"
            f"Peer review:\n{review}\n\n"
            f"Direct response:\n{response}",
        )

    store.update_agent_assignment(
        workspace_id, leader_assignment_id, "completed", report
    )
    store.set_agent_discussion_status(
        workspace_id, discussion_id, "completed"
    )

    return {
        "workspace_id": workspace_id,
        "discussion_id": discussion_id,
        "conversation_id": conversation_id,
        "leader": {
            "id": leader_id,
            "name": "Sovereign Lead",
            "provider": final_info.provider,
            "model": final_info.model,
        },
        "proposals": proposals,
        "peer_reviews": reviews,
        "agent_responses": agent_responses,
        "assignments": store.list_agent_assignments(
            workspace_id, discussion_id
        ),
        "report": report,
        "status": "completed",
    }
