# Import all strategy modules so their @register decorators fire at package import time.
from src.strategies.wheel import strategy as _wheel  # noqa: F401
from src.strategies.spreads import strategy as _spreads  # noqa: F401
from src.strategies.momentum import rsi2 as _rsi2  # noqa: F401
