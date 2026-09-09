"""하위 호환용. 새 코드는 hhhs_db_manager 를 쓰세요.

    python db.py                  -> hhhs-db check
    python db.py "SELECT ..."     -> hhhs-db query "SELECT ..."
"""
import sys

from hhhs_db_manager import *  # noqa: F401,F403
from hhhs_db_manager import get_list_tables as tables, main  # noqa: F401

if __name__ == "__main__":
    sys.exit(main(["query", *sys.argv[1:]] if sys.argv[1:] else ["check"]))
