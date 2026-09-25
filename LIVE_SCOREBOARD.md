# In-play win-expectancy scoreboard

What one contract TAKEN at the recorded price would actually have paid, taker fee included. Settled markets only.

| bucket | n | mean c/contract | total $ | win % | ROI |
|---|---:|---:|---:|---:|---:|
| all comparisons | 1874 | -2.02 | -37.90 | 51% | -4.0% |
| edge > 0 after fees | 738 | -3.23 | -23.80 | 45% | -6.9% |
| edge >= 5c | 403 | -3.72 | -15.00 | 41% | -8.5% |
| edge >= 7c (trade gate) | 294 | -5.67 | -16.67 | 37% | -13.6% |
| edge >= 10c | 186 | -8.01 | -14.91 | 33% | -20.4% |
| edge >= 15c | 90 | -15.55 | -13.99 | 20% | -45.4% |

Brier score (lower is better) — model 0.2258 vs market 0.2038.

**The claimed edge is anti-predictive.** Results get WORSE as the edge threshold rises, so the model's disagreement with the price measures the model's error, not the market's. The market is the better calibrated forecaster. Do not trade this.
