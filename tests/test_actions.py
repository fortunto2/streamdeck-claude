# tests/test_actions.py
from unittest.mock import patch, MagicMock


def test_tmux_send():
    with patch("subprocess.run") as mock_run:
        from src.actions import tmux_send
        tmux_send(target="claude:0", command="/build")
        mock_run.assert_called_once_with(
            ["tmux", "send-keys", "-t", "claude:0", "/build", "Enter"],
            timeout=5,
        )


def test_claude_p():
    with patch("subprocess.Popen") as mock_popen:
        from src.actions import claude_p
        claude_p(prompt="summarize project", allowed_tools="Read")
        args = mock_popen.call_args
        cmd = args[0][0]
        assert cmd[0] == "claude"
        assert "-p" in cmd
        assert "summarize project" in cmd
        assert "--allowedTools" in cmd


def test_shell_exec():
    with patch("subprocess.run") as mock_run:
        from src.actions import shell_exec
        shell_exec("git pull --rebase")
        mock_run.assert_called_once_with(
            "git pull --rebase", shell=True, timeout=30,
        )


def test_tmux_select():
    with patch("subprocess.run") as mock_run:
        from src.actions import tmux_select
        tmux_select(pane="0")
        mock_run.assert_called_once_with(
            ["tmux", "select-pane", "-t", "0"], timeout=5,
        )
