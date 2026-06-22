from typing import Any, Dict, List, Optional, Set, Tuple


def build_account_key(username: str, platform: Optional[str]) -> str:
    normalized_platform = (platform or "twitter").strip() or "twitter"
    return f"{normalized_platform}:{username}".lower()


def sort_accounts_for_platform(
    platform: str,
    accounts: List[Dict[str, Any]],
    db_manager: Any,
    priority_config: Dict[str, Any],
    runtime_prioritized_accounts: Optional[Set[str]] = None,
) -> Tuple[List[Dict[str, Any]], Optional[str]]:
    if not accounts:
        return [], None

    runtime_keys = set(runtime_prioritized_accounts or set())
    priority_enabled = bool(priority_config.get("enabled", False))
    window_days = priority_config.get("window_days", 7)

    def runtime_tier(account: Dict[str, Any]) -> int:
        key = build_account_key(account["username"], account.get("platform", platform))
        return 0 if key in runtime_keys else 1

    count_funcs = {
        "twitter": lambda acc: db_manager.get_recent_post_count_twitter(acc["username"], window_days),
        "pixiv": lambda acc: db_manager.get_recent_post_count_pixiv(acc["username"], window_days),
        "kemono": lambda acc: db_manager.get_recent_post_count_kemono(acc["username"], window_days),
        "tinami": lambda acc: db_manager.get_recent_post_count_tinami(acc["username"], window_days),
        "poipiku": lambda acc: db_manager.get_recent_post_count_poipiku(acc["username"], window_days),
        "fantia": lambda acc: db_manager.get_recent_post_count_fantia(acc["username"], window_days),
        "nijie": lambda acc: db_manager.get_recent_post_count_nijie(acc["username"], window_days),
        "skeb": lambda acc: db_manager.get_recent_post_count_skeb(acc["username"], window_days),
        "misskey": lambda acc: db_manager.get_recent_post_count_misskey(acc["username"], window_days),
        "fanbox": lambda acc: db_manager.get_recent_post_count_fanbox(acc["username"], window_days),
    }

    indexed = list(enumerate(accounts))

    if priority_enabled and platform in count_funcs:
        get_count = count_funcs[platform]
        sort_data: Dict[str, Tuple[int, int, bool]] = {}
        for acc in accounts:
            identifier = acc["username"]
            has_posts = db_manager.has_any_posts(identifier, platform)
            recent_count = get_count(acc)
            priority_tier = 0 if not has_posts else 1
            sort_data[identifier] = (priority_tier, recent_count, has_posts)

        indexed.sort(
            key=lambda item: (
                runtime_tier(item[1]),
                sort_data[item[1]["username"]][0],
                -sort_data[item[1]["username"]][1],
                item[0],
            )
        )
        sorted_accounts = [acc for _, acc in indexed]

        details = []
        for acc in sorted_accounts:
            tier, count, has_posts = sort_data[acc["username"]]
            flags = []
            if runtime_tier(acc) == 0:
                flags.append("runtime")
            if tier == 0 and not has_posts:
                flags.append("new")
            else:
                flags.append(f"{count}posts/{window_days}d")
            details.append(f"{acc['username']}({'/'.join(flags)})")
        return sorted_accounts, f"Priority sort [{platform}]: {', '.join(details)}"

    if runtime_keys:
        indexed.sort(key=lambda item: (runtime_tier(item[1]), item[0]))
        sorted_accounts = [acc for _, acc in indexed]
        details = []
        for acc in sorted_accounts:
            label = "runtime" if runtime_tier(acc) == 0 else "existing"
            details.append(f"{acc['username']}({label})")
        return sorted_accounts, f"Runtime priority [{platform}]: {', '.join(details)}"

    return list(accounts), None
