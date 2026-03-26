import logging
import sys
import os
from pathlib import Path
import colorama
from colorama import Fore, Style

# Initialize colorama
colorama.init(autoreset=True)

# Define colors for different log levels
log_level_colors = {
  logging.DEBUG: Fore.BLUE,
  logging.INFO: Fore.WHITE,
  logging.WARNING: Fore.YELLOW,
  logging.ERROR: Fore.RED,
  logging.CRITICAL: Fore.MAGENTA
}

class ColoredFormatter(logging.Formatter):
  def format(self, record):
    # Get the color for the log level
    level_color = log_level_colors.get(record.levelno, Fore.WHITE)  # Default to white if level not found
    
    # Format the log message
    log_message = super().format(record)
    return f"{level_color}{log_message}{Style.RESET_ALL}"

class CustomLogger:
  def __init__(self, name, logfile_name:str="appcollector.log"):
    self.logger = logging.getLogger(name)
    self.logger.setLevel(logging.DEBUG)

    # Create console handler
    ch = logging.StreamHandler(sys.stdout)
    ch.setLevel(logging.DEBUG)
    # Create a formatter with the current log level colors
    formatter = ColoredFormatter('%(asctime)s - [%(name)s] - %(levelname)s - %(message)s', 
                                 datefmt='%Y-%m-%d %H:%M:%S')
    ch.setFormatter(formatter)


    # File handler
    # --- Determine logs directory one level above this file ---
    base_dir = Path(__file__).resolve().parent.parent
    logs_dir = base_dir / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    logfile = logs_dir / logfile_name
    fh = logging.FileHandler(logfile, mode='a') #rewrite the logfile always
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(logging.Formatter('%(asctime)s - [%(name)s] - %(levelname)s - %(message)s', 
                                      datefmt='%Y-%m-%d %H:%M:%S'))

    # Add the handler to the logger
    self.logger.addHandler(ch)
    self.logger.addHandler(fh)

  def debug(self, message, exc_info=False): 
    self.logger.debug(message, exc_info=exc_info) 

  def info(self, message, exc_info=False): 
    self.logger.info(message, exc_info=exc_info) 

  def warning(self, message, exc_info=False): 
    self.logger.warning(message, exc_info=exc_info)

  def error(self, message, exc_info=False): 
    self.logger.error(message, exc_info=exc_info) 

  def critical(self, message, exc_info=False): 
    self.logger.critical(message, exc_info=exc_info)

# Example usage
if __name__ == "__main__":
  logger1 = CustomLogger('MyClass')
  logger2 = CustomLogger('AnotherClass')
  logger3 = CustomLogger('ThirdClass')

  logger1.debug("This is a debug message from MyClass.")
  logger1.info("This is an info message from MyClass.")
  logger2.warning("This is a warning message from AnotherClass.")
  logger3.error("This is an error message from ThirdClass.")
  logger1.critical("This is a critical message from MyClass.")
  logger2.info("This is an info message from AnotherClass.")

  try:
    a=1 / 0 
  except ZeroDivisionError as e:
    logger3.error(f"An error occurred during division: {e}", exc_info=True) # NEW: Pass exc_info=True

  try:
      int("hello")
  except ValueError as e:
      logger2.warning(f"Conversion failed: {e}", exc_info=True)