# In-play win-expectancy scoreboard

What one contract TAKEN at the recorded price would actually have paid, taker fee included. Settled markets only.

| bucket | n | mean c/contract | total $ | win % | ROI |
|---|---:|---:|---:|---:|---:|
| all comparisons | 1979 | -1.95 | -38.51 | 51% | -3.8% |
| edge > 0 after fees | 784 | -3.65 | -28.59 | 45% | -7.8% |
| edge >= 5c | 421 | -4.25 | -17.91 | 41% | -9.7% |
| edge >= 7c (trade gate) | 307 | -5.81 | -17.83 | 37% | -13.9% |
| edge >= 10c | 193 | -8.73 | -16.84 | 32% | -22.2% |
| edge >= 15c | 95 | -15.82 | -15.03 | 20% | -45.9% |

Brier score (lower is better) — model 0.2246 vs market 0.2017.

3 comparison(s) not yet settled and excluded rather than counted as losses.

**The claimed edge is anti-predictive.** Results get WORSE as the edge threshold rises, so the model's disagreement with the price measures the model's error, not the market's. The market is the better calibrated forecaster. Do not trade this.
