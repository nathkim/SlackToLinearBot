def percent_done(issues: list[dict]) -> float:
    """
    Calculate the percentage of issues marked as "Done" in Linear.
    """
    if not issues:
        return 0.0
    done_count = sum(1 for issue in issues if issue.get("state", {}).get("name") == "Done")
    return (done_count / len(issues)) * 100
