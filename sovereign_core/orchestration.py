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



def resume_collaboration(
    store, router, workspace, run_id: str, discussion_id: str,
    task: str, conversation_id: str | None = None,
    memory_context: str = "", max_agents: int = 4,
    specialist_ids: list[str] | None = None,
) -> dict:
    """Execute or resume a collaboration using durable per-agent checkpoints."""
    import json
    from langchain_core.messages import HumanMessage, SystemMessage

    task = (task or "").strip()
    workspace_id = _get(workspace, "id")
    if not task or not workspace_id or not run_id or not discussion_id:
        raise ValueError("A task, workspace, run and discussion are required.")
    run = store.get_agent_collaboration_run(workspace_id, run_id)
    if not run or run["discussion_id"] != discussion_id:
        raise ValueError("Collaboration run not found in this workspace.")
    if run["status"] == "completed":
        raise ValueError("This collaboration run is already completed.")
    candidates = getattr(router, "candidates", [])
    if not candidates:
        raise RuntimeError("No configured model candidates are available.")

    workspace_name = _get(workspace, "name", "SovereignOS")
    workspace_purpose = _get(workspace, "purpose", "")
    existing = store.list_agent_identities(workspace_id)

    def identity_for(role, name, mission, info):
        found = next((item for item in existing
                      if _get(item, "role") == role
                      and _get(item, "name") == name), None)
        if found:
            return found
        created = store.create_agent_identity(
            workspace_id=workspace_id, role=role, name=name,
            mission=mission, provider=info.provider, model=info.model,
            runtime=getattr(info, "runtime", ""),
        )
        existing.append(created)
        return created

    roster = []
    seen = set()
    for index, candidate in enumerate(candidates):
        specialist = specialist_for(index, candidate)
        key = (specialist["role"], specialist["name"])
        if key not in seen:
            seen.add(key)
            roster.append((specialist, candidate))
    selected_ids = specialist_ids if specialist_ids is not None else run.get("specialist_ids")
    if selected_ids:
        requested = list(dict.fromkeys(selected_ids))
        if len(requested) < 2 or len(requested) > max_agents:
            raise ValueError("The saved Specialist selection is invalid.")
        available = {item[0]["id"] for item in roster}
        unknown = set(requested) - available
        if unknown:
            raise ValueError("Unknown specialist selection: " + ", ".join(sorted(unknown)))
        roster = [item for item in roster if item[0]["id"] in requested]
    roster = roster[:max(1, min(int(max_agents), 4))]
    if not roster:
        raise RuntimeError("No worker agents could be assigned.")

    leader = identity_for(
        "leader", "Sovereign Lead",
        "Coordinate agents, resolve disagreements and verify the final report.",
        candidates[0].info,
    )
    leader_id = _get(leader, "id")
    specialists = [
        (spec, identity_for(
            spec["role"], spec["name"], spec["mission"], candidate.info
        ))
        for spec, candidate in roster
    ]
    assignments = {
        item["agent_id"]: item
        for item in store.list_agent_assignments(workspace_id, discussion_id)
    }
    assignment_ids = {}
    for spec, identity in specialists:
        agent_id = _get(identity, "id")
        assignment = assignments.get(agent_id) or store.create_agent_assignment(
            workspace_id, discussion_id, agent_id,
            f"Provide an independent {spec['role']} analysis, review colleagues' "
            "proposals, and contribute evidence, risks and recommendations.",
        )
        assignment_ids[agent_id] = _get(assignment, "id")
    leader_assignment = assignments.get(leader_id) or store.create_agent_assignment(
        workspace_id, discussion_id, leader_id,
        "Review proposals and peer reviews, resolve or disclose disagreements, "
        "verify the response, and produce the consolidated report.",
    )
    leader_assignment_id = _get(leader_assignment, "id")

    def set_assignment(agent_id, status, result=""):
        assignment_id = assignment_ids.get(agent_id)
        if assignment_id:
            store.update_agent_assignment(
                workspace_id, assignment_id, status, result
            )
        elif agent_id == leader_id:
            store.update_agent_assignment(
                workspace_id, leader_assignment_id, status, result
            )

    messages = store.list_agent_messages(workspace_id, discussion_id)
    if not any(m.get("speaker_type") == "user" and m.get("content") == task
               for m in messages):
        store.append_agent_message(workspace_id, discussion_id, "user", task)

    context = (
        f"Workspace: {workspace_name}\nWorkspace purpose: {workspace_purpose}\n"
        f"Relevant workspace memories:\n{memory_context or '(none)'}\n"
        f"User task:\n{task}"
    )

    def checkpoint_reply(stage, agent_id, speaker_type, system, prompt, start_index):
        checkpoint = store.create_agent_collaboration_checkpoint(
            workspace_id, run_id, discussion_id, stage, agent_id
        )
        if checkpoint["status"] == "completed":
            return checkpoint["result"], checkpoint["provider"], checkpoint["model"]
        store.update_agent_collaboration_checkpoint(
            workspace_id, checkpoint["id"], "in_progress",
            increment_attempts=True,
        )
        try:
            response, info, _ = router.invoke(
                [SystemMessage(content=system), HumanMessage(content=prompt)],
                start_index=start_index,
            )
            reply = _content(response)
            store.complete_agent_collaboration_checkpoint(
                workspace_id, checkpoint["id"], reply, speaker_type,
                agent_id, provider=info.provider, model=info.model,
            )
            return reply, info.provider, info.model
        except Exception as exc:
            store.update_agent_collaboration_checkpoint(
                workspace_id, checkpoint["id"], "failed",
                last_error=type(exc).__name__ + ": " + str(exc),
            )
            raise

    store.update_agent_collaboration_run(
        workspace_id, run_id, status="running", current_stage="proposals",
        last_error="", increment_attempts=True,
    )
    try:
        proposals = []
        for spec, identity in specialists:
            agent_id = _get(identity, "id")
            set_assignment(agent_id, "in_progress")
            system = (
                specialist_prompt(spec, workspace_name)
                + "\nYou are participating in a real multi-agent discussion. "
                "Give an independent proposal. State assumptions, evidence, "
                "risks and questions. Do not claim actions were executed."
            )
            reply, provider, model = checkpoint_reply(
                "proposals", agent_id, "agent", system, context, spec["index"]
            )
            proposals.append({
                "agent_id": agent_id, "name": spec["name"], "role": spec["role"],
                "provider": provider, "model": model, "reply": reply,
            })

        store.update_agent_collaboration_run(
            workspace_id, run_id, current_stage="reviews"
        )
        reviews = []
        for spec, identity in specialists:
            agent_id = _get(identity, "id")
            peers = "\n\n".join(
                f"{p['name']} ({p['role']}):\n{p['reply']}"
                for p in proposals if p["agent_id"] != agent_id
            ) or "(No other agents were assigned.)"
            system = (
                specialist_prompt(spec, workspace_name)
                + "\nReview colleagues' proposals directly. Identify errors, "
                "missing evidence, risks and disagreements. Improve the combined "
                "solution; do not merely repeat your first answer."
            )
            reply, provider, model = checkpoint_reply(
                "reviews", agent_id, "agent", system,
                context + "\n\nColleagues' proposals:\n" + peers,
                spec["index"],
            )
            reviews.append({
                "agent_id": agent_id, "name": spec["name"],
                "provider": provider, "model": model, "reply": reply,
            })

        store.update_agent_collaboration_run(
            workspace_id, run_id, current_stage="responses"
        )
        responses = []
        for index, (spec, identity) in enumerate(specialists):
            agent_id = _get(identity, "id")
            reviewer_spec, reviewer_identity = specialists[(index + 1) % len(specialists)]
            reviewer_id = _get(reviewer_identity, "id")
            review = next(item for item in reviews if item["agent_id"] == reviewer_id)
            system = (
                specialist_prompt(spec, workspace_name)
                + f"\nRespond directly to {reviewer_spec['name']}'s peer review. "
                "Address specific criticisms, explain agreement or disagreement, "
                "correct weaknesses and identify evidence that could change your view."
            )
            history = store.list_agent_messages(workspace_id, discussion_id)
            history_text = "\n\n".join(
                f"{m['speaker_type']} ({m.get('agent_id') or 'user'}):\n{m['content']}"
                for m in history
            )
            prompt = (
                context + "\n\nPersisted discussion so far:\n" + history_text
                + f"\n\nYour colleague {reviewer_spec['name']} provided this peer review:\n"
                + review["reply"] + "\n\nRespond specifically to that colleague."
            )
            reply, provider, model = checkpoint_reply(
                "responses", agent_id, "agent", system, prompt, spec["index"]
            )
            responses.append({
                "agent_id": agent_id, "name": spec["name"], "role": spec["role"],
                "responding_to": reviewer_id, "reviewer_name": reviewer_spec["name"],
                "provider": provider, "model": model, "reply": reply,
            })

        store.update_agent_collaboration_run(
            workspace_id, run_id, current_stage="leader_synthesis"
        )
        team_record = "\n\n".join(
            f"{p['name']} proposal:\n{p['reply']}\nPeer review:\n"
            f"{next(r['reply'] for r in reviews if r['agent_id'] == p['agent_id'])}\n"
            f"Response:\n{next(r['reply'] for r in responses if r['agent_id'] == p['agent_id'])}"
            for p in proposals
        )
        leader_system = (
            f"You are the SovereignOS Leader for the {workspace_name} workspace. "
            "Compare proposals, peer reviews and direct responses. Identify "
            "answered and unanswered criticisms, resolve disagreements only where "
            "evidence permits, disclose unresolved issues, separate facts from "
            "assumptions, identify unverified claims and required human approvals. "
            "Never claim external actions were completed without evidence."
        )
        set_assignment(leader_id, "in_progress")
        report, provider, model = checkpoint_reply(
            "leader_synthesis", leader_id, "leader", leader_system,
            context + "\n\nAgent discussion:\n" + team_record, 0,
        )

        for proposal in proposals:
            agent_id = proposal["agent_id"]
            review = next(x["reply"] for x in reviews if x["agent_id"] == agent_id)
            response = next(x["reply"] for x in responses if x["agent_id"] == agent_id)
            set_assignment(
                agent_id, "completed",
                f"Proposal:\n{proposal['reply']}\n\nPeer review:\n{review}\n\n"
                f"Direct response:\n{response}",
            )
        set_assignment(leader_id, "completed", report)
        store.set_agent_discussion_status(workspace_id, discussion_id, "completed")
        store.update_agent_collaboration_run(
            workspace_id, run_id, status="completed",
            current_stage="completed", last_error="",
        )
        return {
            "run_id": run_id, "workspace_id": workspace_id,
            "discussion_id": discussion_id, "conversation_id": conversation_id,
            "leader": {"id": leader_id, "name": "Sovereign Lead",
                       "provider": provider, "model": model},
            "proposals": proposals, "peer_reviews": reviews,
            "agent_responses": responses,
            "assignments": store.list_agent_assignments(workspace_id, discussion_id),
            "report": report, "status": "completed",
        }
    except Exception as exc:
        store.update_agent_collaboration_run(
            workspace_id, run_id, status="retryable",
            last_error=type(exc).__name__ + ": " + str(exc),
        )
        raise


def run_collaboration(
    store,
    router,
    workspace,
    task: str,
    conversation_id: str | None = None,
    memory_context: str = "",
    max_agents: int = 4,
    specialist_ids: list[str] | None = None,
    run_id: str | None = None,
    discussion_id: str | None = None,
) -> dict:
    """Run proposals, peer review and leader synthesis inside one workspace."""
    if run_id:
        return resume_collaboration(
            store, router, workspace, run_id, discussion_id, task,
            conversation_id, memory_context, max_agents, specialist_ids,
        )
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
