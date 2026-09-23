# In-play win-expectancy scoreboard

What one contract TAKEN at the recorded price would actually have paid, taker fee included. Settled markets only.

| bucket | n | mean c/contract | total $ | win % | ROI |
|---|---:|---:|---:|---:|---:|
| all comparisons | 1793 | -1.83 | -32.88 | 51% | -3.6% |
| edge > 0 after fees | 708 | -3.54 | -25.05 | 45% | -7.5% |
| edge >= 5c | 386 | -4.37 | -16.87 | 41% | -10.0% |
| edge >= 7c (trade gate) | 284 | -5.16 | -14.66 | 38% | -12.4% |
| edge >= 10c | 180 | -8.39 | -15.10 | 33% | -21.1% |
| edge >= 15c | 87 | -15.13 | -13.16 | 21% | -43.9% |

Brier score (lower is better) — model 0.2255 vs market 0.2024.

33 comparison(s) not yet settled and excluded rather than counted as losses.

**The claimed edge is anti-predictive.** Results get WORSE as the edge threshold rises, so the model's disagreement with the price measures the model's error, not the market's. The market is the better calibrated forecaster. Do not trade this.
