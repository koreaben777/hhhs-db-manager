"""오프라인 회귀 테스트. .env/운영 DB에 접근하지 않는다.

실행: .venv/bin/python3 -m unittest -v
"""
import ast
import json
import os
from pathlib import Path
import unittest
from unittest.mock import patch, MagicMock

# import 시 TLS 캐시 파일 생성 방지. 실제 드라이버 연결은 테스트하지 않는다.
os.environ.setdefault("OPENSSL_CONF", os.devnull)
import pandas as pd
import hhhs_db_manager as db


class OfflineTests(unittest.TestCase):
    def setUp(self):
        self.no_engine = patch.object(db, "engine", side_effect=AssertionError("DB access forbidden"))
        self.no_engine.start()
        self.addCleanup(self.no_engine.stop)

    def test_reject_unsafe_sql_before_connection(self):
        statements = [
            "DELETE FROM T", "SELECT 1; DELETE FROM T", "SELECT * INTO X FROM T",
            "WITH x AS (SELECT 1 AS a) DELETE FROM T", "SELECT 1 EXEC p",
            "SELECT * FROM OPENROWSET('x','y','z')", "SELECT 1 /* unfinished",
            "SELECT 'unfinished", "SELECT 1;;", "SELECT 1; SELECT 2", "SELECT NEXT VALUE FOR dbo.seq",
        ]
        for sql in statements:
            with self.subTest(sql=sql), self.assertRaises(db.QueryRejected):
                db.query(sql, allow_heavy=True)

    def test_allow_literals_comments_cte(self):
        for sql in ["SELECT 'DELETE; INTO' AS value; -- ok", "/* a /* b */ c */ SELECT 1",
                    "WITH x AS (SELECT 1 AS a) SELECT a FROM x",
                    "SELECT [UPDATE], 'it''s ok', \"INTO\" FROM T"]:
            with self.subTest(sql=sql):
                db._validate_read_only(sql)

    def test_invalid_limits(self):
        for value in [-1, True, 1.5, "10"]:
            with self.subTest(value=value), self.assertRaises(db.QueryRejected):
                db.query("SELECT 1", max_rows=value)
        for value in [0, -1, True, 1.5]:
            with self.subTest(value=value), self.assertRaises(db.QueryRejected):
                db.get_table("T", value)

    def test_table_preserves_default_cap(self):
        with patch.object(db, "query", return_value=pd.DataFrame()) as query:
            db.get_table("T", 50000)
            self.assertIsNone(query.call_args.kwargs["max_rows"])
            self.assertIn("TOP (50000)", query.call_args.args[0])
            db.get_table("T", 50000, max_rows=20000)
            self.assertEqual(query.call_args.kwargs["max_rows"], 20000)

    def test_cli_table_cap(self):
        with patch.object(db, "get_table", return_value=pd.DataFrame()) as table, patch("builtins.print"):
            self.assertEqual(db.main(["--max-rows", "3", "table", "T", "-n", "20"]), 0)
            self.assertEqual(table.call_args.kwargs["max_rows"], 3)

    def test_dictionary_fallback_contract(self):
        def fake_query(sql, **kwargs):
            if "CM_DICTION" in sql:
                raise db.PermissionDenied("test")
            self.assertIn("AS name_kr", sql)
            return pd.DataFrame({"column": ["ID"], "name_kr": [None], "type": ["int"],
                                 "nullable": ["NO"], "pk": [1]})
        with patch.object(db, "query", side_effect=fake_query):
            result = db.get_columns("T")
            self.assertIn("name_kr", result.columns)
            self.assertEqual(str(result.pk.dtype), "Int64")

    def test_dictionary_timeout_is_not_retried(self):
        with patch.object(db, "query", side_effect=db.QueryTimeout("test")) as query:
            with self.assertRaises(db.QueryTimeout):
                db.get_columns("T")
            self.assertEqual(query.call_count, 1)

    def test_heavy_guard_ignores_comment_and_literal(self):
        catalog = pd.DataFrame({"table": ["BIG_TABLE"]})
        with patch.object(db, "get_list_tables", return_value=catalog):
            for sql in ["SELECT * FROM BIG_TABLE -- WHERE", "SELECT 'TOP' FROM BIG_TABLE"]:
                with self.assertRaises(db.QueryRejected):
                    db._reject_if_heavy(sql)

    def test_explicit_missing_env_does_not_fallback(self):
        with patch.dict(os.environ, {"HHHS_ENV_FILE": "missing-test-env-file"}), patch.object(db, "load_dotenv") as load:
            with self.assertRaises(db.ConfigError):
                db._load_env()
            load.assert_not_called()

    def test_error_does_not_echo_secret(self):
        self.assertNotIn("secret-marker", str(db._translate(Exception("secret-marker"), "SELECT 1")))

    def test_receive_cap_and_binding(self):
        result = MagicMock()
        result.fetchmany.return_value = [(1,), (2,), (3,)]
        result.keys.return_value = ["value"]
        engine = MagicMock()
        connection = engine.connect.return_value.__enter__.return_value
        connection.execute.return_value = result
        with patch.object(db, "engine", return_value=engine):
            with self.assertWarns(UserWarning):
                frame = db._run("SELECT :v AS value", {"v": 1}, 2)
        self.assertEqual(len(frame), 2)
        self.assertTrue(frame.attrs["truncated"])
        result.fetchmany.assert_called_once_with(3)
        result.close.assert_called_once()
        self.assertEqual(connection.execute.call_args.args[1], {"v": 1})

    def test_notebook_save_rejects_existing_and_outside(self):
        notebook = json.loads(Path("hhhs_db_manager.ipynb").read_text())
        code = "".join(notebook["cells"][18]["source"])
        for name in ["../outside.csv", "/outside.csv", "report.txt"]:
            ns = {"df": pd.DataFrame({"a": [1]}), "ask": lambda *args: name,
                  "ROOT": Path.cwd(), "Path": Path}
            with self.subTest(name=name), self.assertRaises(ValueError):
                exec(code, ns)
        ns = {"df": pd.DataFrame({"a": [1]}), "ask": lambda *args: "existing.csv",
              "ROOT": Path.cwd(), "Path": Path}
        with patch.object(Path, "is_symlink", return_value=False), patch.object(Path, "open", side_effect=FileExistsError) as save:
            with self.assertRaises(FileExistsError):
                exec(code, ns)
            save.assert_called_once_with("xb")

    def test_notebook_code_and_outputs(self):
        notebook = json.loads(Path("hhhs_db_manager.ipynb").read_text())
        for cell in notebook["cells"]:
            if cell["cell_type"] == "code":
                ast.parse("".join(cell["source"]))
                self.assertEqual(cell["outputs"], [])
                self.assertIsNone(cell["execution_count"])

    def test_notebook_helpers_without_setup_or_db(self):
        notebook = json.loads(Path("hhhs_db_manager.ipynb").read_text())
        tree = ast.parse("".join(notebook["cells"][2]["source"]))
        functions = ast.Module(body=[n for n in tree.body if isinstance(n, ast.FunctionDef)], type_ignores=[])
        import re
        ns = {"re": re, "db": db}
        exec(compile(functions, "notebook_helpers", "exec"), ns)
        self.assertEqual(ns["full_name"]("NEOE.T"), "[NEOE].[T]")
        for value in ["X]; DELETE FROM T--", "a.b.c", ""]:
            with self.assertRaises(ValueError):
                ns["full_name"](value)
        with patch("builtins.input", side_effect=["-1", "abc", "10001", "20"]), patch("builtins.print"):
            self.assertEqual(ns["ask_limit"](), 20)


if __name__ == "__main__":
    unittest.main()
