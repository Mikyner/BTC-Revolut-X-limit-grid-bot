"""
Grid Engine pro Limit Order Bot
=================================
Zjednodušená oproti market botu:
- Žádná hystereze (limit ordery se vyplní max jednou)
- Žádné find_newly_crossed_level
- Jen build_grid a is_price_outside_grid
"""

import bot.database as db


def build_grid(center_price: float, grid_levels: int, grid_range_percent: float,
               grid_bias_percent: float = 0.0) -> list:
    """
    Vytvoří seznam cenových úrovní.
    grid_bias_percent > 0 = střed dolů = více prostoru pod cenou pro nákupy.
    """
    half_range = center_price * (grid_range_percent / 100) / 2
    bias_eur = half_range * (grid_bias_percent / 100)
    biased_center = center_price - bias_eur
    lower = biased_center - half_range
    upper = biased_center + half_range
    step = (upper - lower) / (grid_levels - 1)
    return [round(lower + i * step, 2) for i in range(grid_levels)]


def is_price_outside_grid(current_price: float, grid_levels: list, max_deviation_percent: float) -> bool:
    if not grid_levels:
        return False
    prices = [l["price"] for l in grid_levels]
    grid_min, grid_max = min(prices), max(prices)
    dev_low = (grid_min - current_price) / grid_min * 100
    dev_high = (current_price - grid_max) / grid_max * 100
    return dev_low > max_deviation_percent or dev_high > max_deviation_percent


def grid_step_eur(grid_price: float, grid_levels: int, grid_range_percent: float) -> float:
    """Vypočítá krok mřížky v EUR pro danou grid cenu."""
    half_range = grid_price * (grid_range_percent / 100) / 2
    return (2 * half_range) / (grid_levels - 1)
