# In-play win-expectancy scoreboard

What one contract TAKEN at the recorded price would actually have paid, taker fee included. Settled markets only.

| bucket | n | mean c/contract | total $ | win % | ROI |
|---|---:|---:|---:|---:|---:|
| all comparisons | 1836 | -1.99 | -36.53 | 51% | -3.9% |
| edge > 0 after fees | 725 | -3.32 | -24.04 | 45% | -7.1% |
| edge >= 5c | 395 | -4.15 | -16.38 | 41% | -9.5% |
| edge >= 7c (trade gate) | 290 | -5.41 | -15.68 | 38% | -13.0% |
| edge >= 10c | 183 | -7.99 | -14.62 | 33% | -20.3% |
| edge >= 15c | 88 | -15.03 | -13.23 | 20% | -44.0% |

Brier score (lower is better) — model 0.2254 vs market 0.2031.

21 comparison(s) not yet settled and excluded rather than counted as losses.

**The claimed edge is anti-predictive.** Results get WORSE as the edge threshold rises, so the model's disagreement with the price measures the model's error, not the market's. The market is the better calibrated forecaster. Do not trade this.
