"""候选杠杆查询。P0：reg 表为空 → 恒 None。P1 归因统计产出候选后生效。"""


def get_active_candidate(storage) -> dict | None:
    row = storage._conn.execute(
        "SELECT lever_key, lever_type, threshold_n, threshold_delta FROM lever_registry "
        "WHERE status = 'candidate' ORDER BY id LIMIT 1"
    ).fetchone()  # P1 加 prior_offset 列
    if row is None:
        return None
    return {
        "lever_key": row[0],
        "lever_type": row[1],
        "threshold_n": row[2],
        "threshold_delta": row[3],
        "prior_offset": row[4] if len(row) > 4 else None,
    }  # P1 加列；P0 恒 None
