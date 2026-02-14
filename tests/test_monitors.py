"""Tests for monitors — git, claude sessions, pipeline, system, tmux."""

from unittest.mock import patch, MagicMock


def test_git_status_clean():
    mock_result = MagicMock()
    mock_result.stdout = ""
    mock_result.returncode = 0
    with patch("subprocess.run", return_value=mock_result):
        from src.monitors import check_git_status
        assert check_git_status("/tmp") == "clean"


def test_git_status_dirty():
    mock_result = MagicMock()
    mock_result.stdout = " M src/file.py\n"
    mock_result.returncode = 0
    with patch("subprocess.run", return_value=mock_result):
        from src.monitors import check_git_status
        assert check_git_status("/tmp") == "dirty"


def test_git_status_untracked():
    mock_result = MagicMock()
    mock_result.stdout = "?? newfile.py\n"
    mock_result.returncode = 0
    with patch("subprocess.run", return_value=mock_result):
        from src.monitors import check_git_status
        assert check_git_status("/tmp") == "untracked"


def test_check_claude_sessions():
    mock_result = MagicMock()
    mock_result.stdout = "1234 claude --dangerously-skip\n5678 claude --dangerously-skip\n"
    mock_result.returncode = 0
    with patch("subprocess.run", return_value=mock_result):
        from src.monitors import check_claude_sessions
        assert check_claude_sessions() == 2


def test_check_pipeline_state_no_files(tmp_path):
    from src.monitors import check_pipeline_state
    assert check_pipeline_state(str(tmp_path)) == "idle"
