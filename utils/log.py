# This program is part of Gluesync.
#
# Bootstrapper is dual-licensed under the following licenses:
#
# 1. GNU General Public License (GPL) Version 3
#    You may use, modify, and distribute this software under the terms of the GPL v3.
#    See the LICENSE-GPL file or <http://www.gnu.org/licenses/gpl-3.0.html> for details.
#    This option is available at no cost, but any derivative works must also be licensed under GPL v3.
#
# 2. MOLO17 Commercial License
#    Alternatively, you may use this software under the MOLO17 Commercial License,
#    which includes a warranty and permits proprietary use. Contact MOLO17 at info@molo17.com
#    for licensing terms and conditions.
#
# You must choose one of these licenses to use this software. Using this software implies
# acceptance of one of these licenses. See the accompanying LICENSE files or contact
# MOLO17 for more information.
#
# Copyright (C) 2025 MOLO17. All rights reserved.

import logging
import os
from logstash_async.handler import AsynchronousLogstashHandler
import sys
from datetime import datetime
from pathlib import Path
from colorama import Back, init

# Initialize colorama
init(autoreset=True)

# Default to INFO if no environment variable is set or if invalid level provided
_logger = None
try:
    LOG_LEVEL = getattr(logging, os.environ.get('LOG_LEVEL', 'DEBUG').upper())
except (AttributeError, ValueError):
    LOG_LEVEL = logging.INFO
    print(f"Invalid log level specified, falling back to INFO", file=sys.stderr)


def get_logger(log_file=None):
    """
    Get or create a logger with both console and file handlers.
    
    Args:
        log_file (str, optional): Path to the log file. If None, only console logging is enabled.
        
    Returns:
        logging.Logger: Configured logger instance
    """
    global _logger
    if not _logger:
        _logger = logging.getLogger("root")
        _logger.setLevel(LOG_LEVEL)
        
        # Clear any existing handlers to avoid duplicates
        if _logger.handlers:
            _logger.handlers.clear()
            
        # Add handlers (console and optionally file)
        add_handlers(_logger, log_file)
    
    return _logger


def add_handlers(logger: logging.Logger, log_file=None):
    """
    Add console and file handlers to the logger.
    
    Args:
        logger (logging.Logger): Logger to add handlers to
        log_file (str, optional): Path to the log file. If None, only console logging is enabled.
    """
    # Console handler
    console_handler = logging.StreamHandler(sys.stderr)
    console_handler.setLevel(logging.DEBUG)
    console_formatter = logging.Formatter(
        "%(asctime)s - %(levelname)s - %(message)s"
    )
    console_handler.setFormatter(console_formatter)
    logger.addHandler(console_handler)
    
    # File handler (if log_file is provided)
    if log_file:
        # Ensure log directory exists
        log_dir = os.path.dirname(log_file)
        if log_dir and not os.path.exists(log_dir):
            os.makedirs(log_dir, exist_ok=True)
            
        file_handler = logging.FileHandler(log_file)
        file_handler.setLevel(logging.DEBUG)
        file_formatter = logging.Formatter(
            "%(asctime)s - %(levelname)s - %(message)s"
        )
        file_handler.setFormatter(file_formatter)
        logger.addHandler(file_handler)

    # Check if running in Docker container
    is_docker = os.path.exists('/.dockerenv')
    
    if is_docker:
        try:
            # Add Logstash handler only in Docker container
            logstash_host = '10.17.3.107'
            logstash_port = 5000
            
            # Initialize Logstash handler with error handling
            logstash_handler = AsynchronousLogstashHandler(
                host=logstash_host,
                port=logstash_port,
                database_path='/tmp/logstash_events.db',  # Using /tmp for better Docker compatibility
                ssl_enable=False,  # Disable SSL by default
                ssl_verify=False,  # Disable SSL verification
                transport='logstash_async.transport.TcpTransport',
                ssl_details=None
            )
            logstash_handler.setLevel(logging.DEBUG)
            
            # Add extra context to all log records
            class ContextFilter(logging.Filter):
                def filter(self, record):
                    record.extra_fields = {
                        'appname': 'gluesync-bootstrapper',
                        'environment': 'INTEGRATION_TEST',
                        'user': {'name': 'MOLO17'},
                        'test_name': os.environ.get('TEST_NAME', 'not_set'),
                        'job_id': os.environ.get('JOB_ID', 'not_set'),
                        'version': os.environ.get('VERSION', 'not_set')
                    }
                    return True

            # Avoid adding the filter multiple times
            if not any(isinstance(f, ContextFilter) for f in logger.filters):
                logger.addFilter(ContextFilter())
                
            # Test the connection by sending a test message
            logstash_handler.emit(logging.makeLogRecord({
                'msg': 'Testing Logstash connection',
                'levelno': logging.INFO,
                'levelname': 'INFO'
            }))
            
            logger.addHandler(logstash_handler)
            logger.info("Logstash logging enabled (Docker container detected)")
            
        except Exception as e:
            # Log the error but don't let it crash the application
            logger.warning(f"Failed to initialize Logstash logging: {str(e)}. Continuing without Logstash logging.")
            logger.debug("Logstash connection error details:", exc_info=True)
    else:
        logger.debug("Logstash logging disabled (not running in Docker container)")


def create_log_file(log_dir=None):
    """
    Create a log file with a timestamp in the specified directory.
    
    Args:
        log_dir (str, optional): Directory to create the log file in.
                                If None, checks for LOG_DIR environment variable.
                                If LOG_DIR is not set, uses 'logs' directory in the current directory.
                                
    Returns:
        str: Path to the created log file
    """
    if not log_dir:
        # Check for LOG_DIR environment variable first
        default_log_dir = os.environ.get('LOG_DIR')
        if not default_log_dir:
            # Use appropriate default based on platform
            if os.path.exists('/logs'):
                # Docker/Linux environment
                default_log_dir = '/logs'
            elif sys.platform.startswith('win'):
                # Windows - prefer LOCALAPPDATA when available
                local_appdata = os.environ.get('LOCALAPPDATA')
                if local_appdata:
                    default_log_dir = os.path.join(local_appdata, 'GluesyncAutomator', 'Logs')
                else:
                    default_log_dir = os.path.join(os.path.expanduser('~'), 'AppData', 'Local', 'GluesyncAutomator', 'Logs')
            else:
                # macOS / other Unix-like environments - use user's Library/Logs
                default_log_dir = os.path.join(os.path.expanduser('~'), 'Library', 'Logs', 'GluesyncAutomator')
        log_dir = default_log_dir
        
    # Ensure log directory exists
    if not os.path.exists(log_dir):
        os.makedirs(log_dir, exist_ok=True)
        
    # Create log file with timestamp
    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    log_file = os.path.join(log_dir, f"log_{timestamp}.log")
    
    return log_file


# Custom logging methods with colored output
def log_success(logger, message):
    """Log a success message with green background for the 'PASS' part"""
    logger.info(Back.GREEN + "PASS" + Back.RESET + " - " + message)


def log_failure(logger, message):
    """Log a failure message with yellow background for the 'ERROR' part"""
    logger.error(Back.YELLOW + "ERROR" + Back.RESET + " - " + message)


def log_fatal(logger, message):
    """Log a fatal error message with red background for the 'FATAL' part"""
    logger.critical(Back.RED + "FATAL" + Back.RESET + " - " + message)


def create_lockfile_dir():
    """Create the logs directory if it doesn't exist"""
    # Use user's Library/Logs directory instead of current working directory
    # to avoid read-only filesystem issues in macOS app bundles
    logs_dir = os.path.join(os.path.expanduser("~"), "Library", "Logs", "GluesyncAutomator", "locks")
    os.makedirs(logs_dir, exist_ok=True)
    return logs_dir


def lockfile_complete():
    """Create a lockfile indicating successful completion"""
    logs_dir = create_lockfile_dir()
    complete_lock = Path(os.path.join(logs_dir, "complete.lck"))
    complete_lock.touch()
    os.chmod(complete_lock, 0o666)


def lockfile_failure():
    """Create a lockfile indicating failure"""
    logs_dir = create_lockfile_dir()
    failure_lock = Path(os.path.join(logs_dir, "failure.lck"))
    failure_lock.touch()
    os.chmod(failure_lock, 0o666)


def exit_on_fail():
    """Create a failure lockfile and exit with error code"""
    lockfile_failure()
    sys.exit(1)


def log_warning(logger, message):
    """Log a warning message"""
    logger.warning(message)


def log_info(logger, message):
    """Log an info message"""
    logger.info(message)
