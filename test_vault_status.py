import os, sys
sys.path.insert(0, os.path.dirname(__file__))
from vault_sync import new_status, READ


def test_boxes_decide_the_status():
    # (status, done_box, read_box, prev_done, prev_read) -> status
    assert new_status("nueva", True, False, False, False) == "hecha"      # ticked Completada
    assert new_status("hecha", False, True, True, True) == READ           # unticked Completada, Leída still ticked
    assert new_status("hecha", False, False, True, False) == "nueva"      # unticked both
    assert new_status("nueva", False, True, False, False) == READ         # ticked Leída
    assert new_status(READ, False, False, False, True) == "nueva"         # unticked Leída
    assert new_status("hecha", True, False, True, True) == "hecha"        # Leída unticked under Completada: stays done
    assert new_status(READ, False, False, False, False) == READ           # status changed elsewhere (opened): boxes follow
    assert new_status("hecha", False, False, False, False) == "hecha"     # ticked in the HA list: boxes follow
