# In-play win-expectancy scoreboard

What one contract TAKEN at the recorded price would actually have paid, taker fee included. Settled markets only.

| bucket | n | mean c/contract | total $ | win % | ROI |
|---|---:|---:|---:|---:|---:|
| all comparisons | 1826 | -1.99 | -36.32 | 51% | -3.9% |
| edge > 0 after fees | 723 | -3.34 | -24.14 | 45% | -7.1% |
| edge >= 5c | 393 | -4.19 | -16.47 | 41% | -9.6% |
| edge >= 7c (trade gate) | 289 | -5.26 | -15.20 | 38% | -12.7% |
| edge >= 10c | 183 | -7.99 | -14.62 | 33% | -20.3% |
| edge >= 15c | 88 | -15.03 | -13.23 | 20% | -44.0% |

Brier score (lower is better) — model 0.2256 vs market 0.2032.

**The claimed edge is anti-predictive.** Results get WORSE as the edge threshold rises, so the model's disagreement with the price measures the model's error, not the market's. The market is the better calibrated forecaster. Do not trade this.
