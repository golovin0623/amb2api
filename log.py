"""
日志模块 - 使用环境变量配置
"""
import os
import sys
import threading
from datetime import datetime

# 日志级别定义
LOG_LEVELS = {
    'debug': 0,
    'info': 1,
    'warning': 2,
    'error': 3,
    'critical': 4
}

# 线程锁，用于文件写入同步
_file_lock = threading.Lock()

# 文件写入状态标志
_file_writing_disabled = False
_disable_reason = None

# 持久文件句柄 + 字节计数（避免每行 open()/getsize 的系统调用开销）
_log_fh = None
_log_fh_path = None
_current_size = 0

def _get_current_log_level():
    """获取当前日志级别"""
    level = os.getenv('LOG_LEVEL', 'info').lower()
    return LOG_LEVELS.get(level, LOG_LEVELS['info'])

def _get_log_file_path():
    """获取日志文件路径"""
    return os.getenv('LOG_FILE', 'log.txt')

def _get_max_bytes() -> int:
    """单个日志文件最大字节数，超过则轮转。0 表示不轮转。默认 10MB。"""
    try:
        return int(os.getenv('LOG_MAX_BYTES', str(10 * 1024 * 1024)))
    except ValueError:
        return 10 * 1024 * 1024

def _get_backup_count() -> int:
    """保留的历史日志份数（log.txt.1 ... log.txt.N）。默认 3。"""
    try:
        return max(0, int(os.getenv('LOG_BACKUP_COUNT', '3')))
    except ValueError:
        return 3

def _close_handle():
    global _log_fh
    if _log_fh is not None:
        try:
            _log_fh.close()
        except Exception:
            pass
        _log_fh = None

def _rotate(log_file: str):
    """size-based 轮转：log.txt -> log.txt.1 -> ... -> log.txt.N（删除最旧）。"""
    _close_handle()
    backup = _get_backup_count()
    if backup <= 0:
        try:
            if os.path.exists(log_file):
                os.remove(log_file)
        except OSError:
            pass
        return
    try:
        oldest = f"{log_file}.{backup}"
        if os.path.exists(oldest):
            os.remove(oldest)
    except OSError:
        pass
    for i in range(backup - 1, 0, -1):
        src, dst = f"{log_file}.{i}", f"{log_file}.{i + 1}"
        if os.path.exists(src):
            try:
                os.replace(src, dst)
            except OSError:
                pass
    try:
        if os.path.exists(log_file):
            os.replace(log_file, f"{log_file}.1")
    except OSError:
        pass

def _get_handle(log_file: str):
    """返回持久文件句柄；路径变化或首次打开时重开并刷新字节计数。"""
    global _log_fh, _log_fh_path, _current_size
    if _log_fh is not None and _log_fh_path == log_file and not _log_fh.closed:
        return _log_fh
    _close_handle()
    _log_fh = open(log_file, 'a', encoding='utf-8')
    _log_fh_path = log_file
    try:
        _current_size = os.path.getsize(log_file)
    except OSError:
        _current_size = 0
    return _log_fh

def _write_to_file(message: str):
    """线程安全地写入日志文件（带 size 轮转，持久句柄）。"""
    global _file_writing_disabled, _disable_reason, _current_size

    if _file_writing_disabled:
        return

    try:
        log_file = _get_log_file_path()
        data = message + '\n'
        encoded_len = len(data.encode('utf-8'))
        with _file_lock:
            max_bytes = _get_max_bytes()
            # 仅用内存计数判断是否需要轮转，避免每行 getsize 系统调用
            if max_bytes > 0 and _log_fh is not None and _current_size + encoded_len > max_bytes:
                _rotate(log_file)  # 关闭句柄；下面 _get_handle 会重开
            fh = _get_handle(log_file)
            fh.write(data)
            fh.flush()  # 保留实时刷新，便于面板日志流
            _current_size += encoded_len
    except (PermissionError, OSError, IOError) as e:
        _file_writing_disabled = True
        _disable_reason = str(e)
        print(f"Warning: File system appears to be read-only or permission denied. Disabling log file writing: {e}", file=sys.stderr)
        print(f"Log messages will continue to display in console only.", file=sys.stderr)
    except Exception as e:
        print(f"Warning: Failed to write to log file: {e}", file=sys.stderr)

def _log(level: str, message: str):
    """
    内部日志函数
    """
    level = level.lower()
    if level not in LOG_LEVELS:
        print(f"Warning: Unknown log level '{level}'", file=sys.stderr)
        return
    
    # 检查日志级别
    current_level = _get_current_log_level()
    if LOG_LEVELS[level] < current_level:
        return
    
    # 格式化日志消息
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    entry = f"[{timestamp}] [{level.upper()}] {message}"
    
    # 输出到控制台
    if level in ('error', 'critical'):
        print(entry, file=sys.stderr)
    else:
        print(entry)
    
    # 实时写入文件
    _write_to_file(entry)

def set_log_level(level: str):
    """设置日志级别提示"""
    level = level.lower()
    if level not in LOG_LEVELS:
        print(f"Warning: Unknown log level '{level}'. Valid levels: {', '.join(LOG_LEVELS.keys())}")
        return False
    
    print(f"Note: To set log level '{level}', please set LOG_LEVEL environment variable")
    return True

class Logger:
    """支持 log('info', 'msg') 和 log.info('msg') 两种调用方式"""
    
    def __call__(self, level: str, message: str):
        """支持 log('info', 'message') 调用方式"""
        _log(level, message)

    def debug(self, message: str):
        """记录调试信息"""
        _log('debug', message)
    
    def info(self, message: str):
        """记录一般信息"""
        _log('info', message)
    
    def warning(self, message: str):
        """记录警告信息"""
        _log('warning', message)
    
    def error(self, message: str):
        """记录错误信息"""
        _log('error', message)
    
    def critical(self, message: str):
        """记录严重错误信息"""
        _log('critical', message)
    
    def get_current_level(self) -> str:
        """获取当前日志级别名称"""
        current_level = _get_current_log_level()
        for name, value in LOG_LEVELS.items():
            if value == current_level:
                return name
        return 'info'
    
    def get_log_file(self) -> str:
        """获取当前日志文件路径"""
        return _get_log_file_path()
    

# 导出全局日志实例
log = Logger()

# 导出的公共接口
__all__ = ['log', 'set_log_level', 'LOG_LEVELS']

# 使用说明:
# 1. 设置日志级别: export LOG_LEVEL=debug (或在.env文件中设置)
# 2. 设置日志文件: export LOG_FILE=log.txt (或在.env文件中设置)