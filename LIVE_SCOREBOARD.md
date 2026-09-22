# In-play win-expectancy scoreboard

What one contract TAKEN at the recorded price would actually have paid, taker fee included. Settled markets only.

| bucket | n | mean c/contract | total $ | win % | ROI |
|---|---:|---:|---:|---:|---:|
| all comparisons | 968 | +0.37 | +3.58 | 53% | +0.7% |
| edge > 0 after fees | 389 | -0.17 | -0.64 | 49% | -0.3% |
| edge >= 5c | 212 | -0.78 | -1.66 | 45% | -1.8% |
| edge >= 7c (trade gate) | 158 | -1.70 | -2.69 | 42% | -4.1% |
| edge >= 10c | 103 | -4.71 | -4.85 | 37% | -11.7% |
| edge >= 15c | 51 | -13.21 | -6.74 | 22% | -39.5% |

Brier score (lower is better) — model 0.2268 vs market 0.2027.

836 comparison(s) not yet settled and excluded rather than counted as losses.

**The claimed edge is anti-predictive.** Results get WORSE as the edge threshold rises, so the model's disagreement with the price measures the model's error, not the market's. The market is the better calibrated forecaster. Do not trade this.
