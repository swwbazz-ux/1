"""Arithmetic counterexamples for the proposed timing contract.

This is a standalone semantic model, not a Django, PostgreSQL, browser,
device, or production test. Time is seconds relative to 10:00:00.
"""


def clock(seconds):
    absolute = 10 * 3600 + seconds
    hours, remainder = divmod(absolute, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}"


def proposed_upper(anchor_upper, first_receive):
    return min(anchor_upper, first_receive)


def tighten_parent_child(parent, child):
    pl, pu = parent
    cl, cu = child
    tightened_parent = (pl, min(pu, cu))
    tightened_child = (max(cl, pl), cu)
    if tightened_parent[0] > tightened_parent[1] or tightened_child[0] > tightened_child[1]:
        raise ValueError("inconsistent temporal evidence")
    return tightened_parent, tightened_child


def main():
    print("Standalone arithmetic model; application/DB/device tests NOT_RUN.")
    bounds = (-10, 20)
    due_a = proposed_upper(bounds[1], 5) + 300
    due_b = proposed_upper(bounds[1], 15) + 300
    assert bounds[0] <= 0 <= bounds[1]
    assert due_a != due_b
    print(f"C1: same anchor [09:59:50,10:00:20], receipts 10:00:05/10:00:15 -> deadlines {clock(due_a)}/{clock(due_b)} (different).")

    parent = (-60, proposed_upper(600, 240))
    child = (50, proposed_upper(70, 120))
    assert parent[0] <= 0 <= parent[1]
    assert child[0] <= 60 <= child[1]
    assert 0 < 60
    expanded_child_upper = max(parent[1], child[1])
    assert expanded_child_upper > child[1]
    corrected_parent, corrected_child = tighten_parent_child(parent, child)
    assert corrected_parent == (-60, 70)
    assert corrected_child == (50, 70)
    print(f"C2: causality is valid; upper-parent {clock(parent[1])} > upper-child {clock(child[1])}. Tighten parent to {clock(corrected_parent[1])}; do not inflate child to {clock(expanded_child_upper)}.")

    frozen_before_child = parent[1] + 300
    estimated_after_child = corrected_parent[1] + 300
    assert frozen_before_child != estimated_after_child
    assert tighten_parent_child(corrected_parent, corrected_child) == (corrected_parent, corrected_child)
    print(f"C3: freezing before new evidence keeps {clock(frozen_before_child)}; processing the complete evidence first gives {clock(estimated_after_child)}. Duplicate evidence is idempotent; new evidence can refine the estimate.")

    anchor_only_due_a = bounds[1] + 300
    anchor_only_due_b = bounds[1] + 300
    assert anchor_only_due_a == anchor_only_due_b == 320
    print(f"C4: a deadline derived from the same immutable anchor upper bound alone is {clock(anchor_only_due_a)} in both deliveries. This does not settle the no-anchor policy.")
    print("4 arithmetic checks completed; counterexamples confirmed. No integration acceptance asserted.")


if __name__ == "__main__":
    main()
