"""日志按大小轮转，不再无限增长。"""
import importlib
import os


def test_log_rotates_by_size(tmp_path, monkeypatch):
    log_file = tmp_path / "app.log"
    monkeypatch.setenv("LOG_FILE", str(log_file))
    monkeypatch.setenv("LOG_MAX_BYTES", "2048")  # 2KB
    monkeypatch.setenv("LOG_BACKUP_COUNT", "2")
    monkeypatch.setenv("LOG_LEVEL", "info")

    import log as logmod
    importlib.reload(logmod)  # 重置模块级句柄/计数到干净状态

    line = "x" * 200
    for _ in range(60):  # ~12KB，必然多次触发 2KB 轮转
        logmod.log.info(line)
    logmod._close_handle()

    # 主文件存在且被 max_bytes 约束（允许超出最后一行）
    assert log_file.exists()
    assert log_file.stat().st_size <= 2048 + 1024
    # 至少产生 1 个历史份
    assert (tmp_path / "app.log.1").exists()
    # 历史份数不超过 backup_count
    assert not (tmp_path / "app.log.3").exists()


def test_backup_count_zero_truncates(tmp_path, monkeypatch):
    log_file = tmp_path / "app.log"
    monkeypatch.setenv("LOG_FILE", str(log_file))
    monkeypatch.setenv("LOG_MAX_BYTES", "1024")
    monkeypatch.setenv("LOG_BACKUP_COUNT", "0")
    monkeypatch.setenv("LOG_LEVEL", "info")

    import log as logmod
    importlib.reload(logmod)

    for _ in range(40):
        logmod.log.info("y" * 100)
    logmod._close_handle()

    assert log_file.exists()
    assert log_file.stat().st_size <= 1024 + 512
    assert not (tmp_path / "app.log.1").exists()


def teardown_module(module):
    # 还原默认日志模块状态，避免影响其他测试的 log 行为
    import log as logmod
    logmod._close_handle()
    importlib.reload(logmod)
