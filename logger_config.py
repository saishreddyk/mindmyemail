import logging
import pytz
from datetime import datetime
from typing import Optional
from colorama import Fore, Style, init

# Initialize colorama
init()

# Color mapping
COLORS = {
    'DEBUG': Fore.BLUE,
    'INFO': Fore.GREEN,
    'WARNING': Fore.YELLOW,
    'ERROR': Fore.RED,
    'CRITICAL': Fore.RED + Style.BRIGHT
}

class ColoredFormatter(logging.Formatter):
    def format(self, record):
        # Get the original format
        record.msg = f"{COLORS.get(record.levelname, '')}{record.msg}{Style.RESET_ALL}"
        
        # Add PST timestamp
        pst = pytz.timezone('US/Pacific')
        record.pst_time = datetime.now(pst).strftime('%Y-%m-%d %H:%M:%S %Z')
        
        return super().format(record)

def setup_logger(name: Optional[str] = None) -> logging.Logger:
    """Setup and return a colored logger with PST timestamp"""
    logger = logging.getLogger(name or __name__)
    
    if not logger.handlers:  # Avoid adding handlers multiple times
        logger.setLevel(logging.DEBUG)
        
        # Create console handler
        console_handler = logging.StreamHandler()
        console_handler.setLevel(logging.DEBUG)
        
        # Create formatter
        formatter = ColoredFormatter(
            '%(pst_time)s [%(levelname)s] %(filename)s:%(lineno)d - %(message)s'
        )
        
        # Add formatter to handler
        console_handler.setFormatter(formatter)
        
        # Add handler to logger
        logger.addHandler(console_handler)
    
    return logger 