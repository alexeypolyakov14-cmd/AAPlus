"""Позиционирование по дюрации в зависимости от фазы цикла ключевой ставки."""
from __future__ import annotations

from .base import MarketContext, Strategy, equal_weights


class RateCycleStrategy(Strategy):
    """Фаза цикла ДКП (по истории решений ЦБ) + наклон кривой -> целевая дюрация -> ОФЗ.

      easing (ЦБ снижает)       -> длинная дюрация (long_dur ± band)
      tightening (ЦБ повышает)  -> короткая дюрация (short_dur ± band), доля флоатеров floater_share (если есть в universe)
      hold                      -> инверсия кривой (10Y−1Y < inversion_pp) означает ожидание снижения -> mid_long_dur,
                                   иначе mid_dur
    Берём top_n ОФЗ-ПД (или корпоратов 1-го уровня при allow_corporate) с дюрацией в целевой полосе, равные веса.
    """
    name = "rate_cycle"

    def __init__(self, short_dur: float = 1.0, mid_dur: float = 2.5, mid_long_dur: float = 4.0, long_dur: float = 6.0,
                 band: float = 1.5, top_n: int = 4, floater_share: float = 0.5, inversion_pp: float = -1.0,
                 allow_corporate: bool = False):
        self.short_dur, self.mid_dur, self.mid_long_dur, self.long_dur = short_dur, mid_dur, mid_long_dur, long_dur
        self.band = band
        self.top_n = top_n
        self.floater_share = floater_share
        self.inversion_pp = inversion_pp
        self.allow_corporate = allow_corporate
        self._reasons: dict[str, str] = {}
        self.last_regime = ""
        self.last_target_duration = 0.0

    def target_duration(self, ctx: MarketContext) -> tuple[float, str]:
        regime = ctx.keyrate.regime if ctx.keyrate else "hold"
        slope = ctx.curve.slope(1, 10) if ctx.curve else 0.0
        if regime == "easing":
            return self.long_dur, f"ЦБ снижает ставку ({ctx.keyrate.current:.2f}%), удлиняем дюрацию"
        if regime == "tightening":
            return self.short_dur, f"ЦБ повышает ставку ({ctx.keyrate.current:.2f}%), укорачиваем дюрацию"
        if slope < self.inversion_pp:
            return self.mid_long_dur, f"пауза ЦБ, инверсия кривой {slope:+.2f} п.п. — рынок ждёт снижения"
        return self.mid_dur, f"пауза ЦБ, наклон кривой {slope:+.2f} п.п. — нейтральная дюрация"

    def targets(self, ctx: MarketContext) -> dict[str, float]:
        self._reasons = {}
        td, why = self.target_duration(ctx)
        self.last_regime = ctx.keyrate.regime if ctx.keyrate else "hold"
        self.last_target_duration = td
        pool = [r for r in ctx.rows if (r.bond.is_ofz or (self.allow_corporate and (r.bond.list_level or 9) == 1))]
        floaters = [r for r in pool if r.bond.is_floater]
        fixed = [r for r in pool if not r.bond.is_floater and not r.bond.is_linker]
        in_band = [r for r in fixed if abs(r.metrics.macaulay_duration - td) <= self.band]
        if not in_band and fixed:
            in_band = sorted(fixed, key=lambda r: abs(r.metrics.macaulay_duration - td))[: self.top_n]
        # приоритет — близость к целевой дюрации (с шагом 0.5 года), затем доходность
        picks = sorted(in_band, key=lambda r: (round(abs(r.metrics.macaulay_duration - td) * 2) / 2, -r.metrics.yield_worst))[: self.top_n]
        fixed_share = 1.0
        out: dict[str, float] = {}
        if self.last_regime == "tightening" and floaters and self.floater_share > 0:
            fl = sorted(floaters, key=lambda r: -r.quote.turnover)[: max(1, self.top_n // 2)]
            out.update(equal_weights([r.secid for r in fl], self.floater_share))
            for r in fl:
                self._reasons[r.secid] = "флоатер: защита от роста ставки"
            fixed_share = 1.0 - self.floater_share
        out.update(equal_weights([r.secid for r in picks], fixed_share))
        for r in picks:
            self._reasons[r.secid] = f"{why}; дюрация {r.metrics.macaulay_duration:.1f} vs цель {td:.1f}"
        return out

    def explain(self, ctx: MarketContext) -> dict[str, str]:
        return dict(self._reasons)
