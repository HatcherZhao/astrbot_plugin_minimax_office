"""MiniMax Office Worker 固定入口的单元测试。"""

import importlib.util
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock


MODULE_PATH = Path(__file__).parents[1] / "worker/run_task.py"
SPEC = importlib.util.spec_from_file_location("office_worker_run_task", MODULE_PATH)
assert SPEC and SPEC.loader
RUNNER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(RUNNER)

HELD_MODULE_PATH = Path(__file__).parents[1] / "worker/run_held_task.py"
HELD_SPEC = importlib.util.spec_from_file_location("office_worker_held_task", HELD_MODULE_PATH)
assert HELD_SPEC and HELD_SPEC.loader
HELD = importlib.util.module_from_spec(HELD_SPEC)
HELD_SPEC.loader.exec_module(HELD)


class WorkerRunnerTest(unittest.TestCase):
    """验证语言白名单、环境清理和固定命令。"""

    def test_rejects_unknown_language(self):
        """未知语言不能进入任何子进程。"""
        with mock.patch.object(RUNNER.subprocess, "run") as run:
            self.assertEqual(RUNNER.main(["bash"]), 64)
            run.assert_not_called()

    def test_environment_does_not_inherit_secrets_or_proxy(self):
        """任务环境不能继承宿主凭据、代理或 Docker 地址。"""
        with mock.patch.dict(
            os.environ,
            {
                "OFFICE_RUNTIME_TOKEN": "secret",
                "HTTP_PROXY": "http://proxy",
                "DOCKER_HOST": "unix:///var/run/docker.sock",
            },
            clear=True,
        ):
            env = RUNNER._base_environment()
        self.assertNotIn("OFFICE_RUNTIME_TOKEN", env)
        self.assertNotIn("HTTP_PROXY", env)
        self.assertNotIn("DOCKER_HOST", env)
        self.assertEqual(env["HOME"], "/tmp/home")

    def test_fixed_python_and_javascript_commands(self):
        """Python 与 Node 只运行契约中的固定源码文件。"""
        env = RUNNER._base_environment()
        self.assertEqual(
            RUNNER._command("python", Path("/work/source/main.py"), env),
            [RUNNER.sys.executable, "-I", "/work/source/main.py"],
        )
        self.assertEqual(
            RUNNER._command("javascript", Path("/work/source/main.js"), env),
            ["node", "--disable-proto=throw", "/work/source/main.js"],
        )

    def test_source_must_be_regular_and_not_symlink(self):
        """固定源码为符号链接时必须拒绝。"""
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            target = root / "actual.py"
            target.write_text("print('x')")
            (root / "main.py").symlink_to(target)
            with mock.patch.object(RUNNER, "SOURCE_ROOT", root):
                with self.assertRaises(RuntimeError):
                    RUNNER._regular_source("python")

    def test_csharp_rejects_dynamic_references(self):
        """C# 不允许通过 #r 或 #load 扩大依赖范围。"""
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp) / "Program.cs"
            source.write_text('#r "nuget: Evil"\nConsole.WriteLine("x");')
            with self.assertRaisesRegex(RuntimeError, "#r or #load"):
                RUNNER._compile_csharp(source, RUNNER._base_environment())

    def test_csharp_region_and_similar_text_are_allowed(self):
        """普通预处理区块与包含 #row 的文本不能被误判为动态引用。"""
        text = '#region 文档\nvar label = "#row";\n#endregion\n'
        self.assertFalse(RUNNER._has_dynamic_reference_directive(text))
        self.assertTrue(RUNNER._has_dynamic_reference_directive('#r "nuget: Package"\n'))
        self.assertTrue(RUNNER._has_dynamic_reference_directive('  #load "helper.csx"\n'))

    def test_main_propagates_child_exit_code(self):
        """固定子进程失败码必须回传给控制器。"""
        fake_source = Path("/work/source/main.py")
        completed = subprocess.CompletedProcess(["python"], 17)
        with (
            mock.patch.object(RUNNER, "_prepare_directories"),
            mock.patch.object(RUNNER, "_regular_source", return_value=fake_source),
            mock.patch.object(RUNNER, "_command", return_value=["python", str(fake_source)]),
            mock.patch.object(RUNNER.subprocess, "run", return_value=completed),
        ):
            self.assertEqual(RUNNER.main(["python"]), 17)

    def test_python_source_runs_without_shell(self):
        """Python 固定入口可直接运行且不会经过 shell。"""
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "source"
            output = root / "output"
            scratch = root / "tmp"
            source.mkdir()
            output.mkdir()
            (source / "main.py").write_text("print('worker-python-ok')")
            with (
                mock.patch.object(RUNNER, "SOURCE_ROOT", source),
                mock.patch.object(RUNNER, "OUTPUT_ROOT", output),
                mock.patch.object(RUNNER, "TEMP_ROOT", scratch),
            ):
                self.assertEqual(RUNNER.main(["python"]), 0)


class HeldWorkerRunnerTest(unittest.TestCase):
    """验证控制器归档所需的完成标志与固定调用。"""

    def setUp(self):
        """重置模块级停止状态，避免测试互相影响。"""
        HELD._stopping = False

    def test_rejects_unknown_language(self):
        """保持入口同样不能接受任意命令。"""
        with mock.patch.object(HELD.subprocess, "run") as run:
            self.assertEqual(HELD.main(["shell"]), 64)
            run.assert_not_called()

    def test_writes_exit_marker_and_uses_fixed_runner(self):
        """任务结束后必须写退出码并只调用固定 runner。"""
        with tempfile.TemporaryDirectory() as temp:
            result_path = Path(temp) / "result.json"
            completed = subprocess.CompletedProcess([HELD.RUNNER, "python"], 23)
            with (
                mock.patch.object(HELD, "RESULT_PATH", result_path),
                mock.patch.object(HELD, "HOLD_SECONDS", 0),
                mock.patch.object(HELD.subprocess, "run", return_value=completed) as run,
            ):
                self.assertEqual(HELD.main(["python"]), 23)
            run.assert_called_once_with([HELD.RUNNER, "python"], check=False)
            self.assertEqual(json.loads(result_path.read_text())["exit_code"], 23)

    def test_launch_failure_is_reported(self):
        """固定 runner 启动异常也要留下可归档的失败码。"""
        with tempfile.TemporaryDirectory() as temp:
            result_path = Path(temp) / "result.json"
            with (
                mock.patch.object(HELD, "RESULT_PATH", result_path),
                mock.patch.object(HELD, "HOLD_SECONDS", 0),
                mock.patch.object(HELD.subprocess, "run", side_effect=OSError("missing")),
            ):
                self.assertEqual(HELD.main(["csharp"]), 70)
            self.assertEqual(json.loads(result_path.read_text())["exit_code"], 70)


if __name__ == "__main__":
    unittest.main()
