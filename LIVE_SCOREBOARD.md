# In-play win-expectancy scoreboard

What one contract TAKEN at the recorded price would actually have paid, taker fee included. Settled markets only.

| bucket | n | mean c/contract | total $ | win % | ROI |
|---|---:|---:|---:|---:|---:|
| all comparisons | 1563 | -1.36 | -21.27 | 52% | -2.6% |
| edge > 0 after fees | 616 | -3.05 | -18.77 | 46% | -6.5% |
| edge >= 5c | 335 | -4.87 | -16.31 | 41% | -11.1% |
| edge >= 7c (trade gate) | 248 | -6.30 | -15.63 | 37% | -15.0% |
| edge >= 10c | 162 | -8.90 | -14.41 | 33% | -22.2% |
| edge >= 15c | 78 | -16.43 | -12.82 | 21% | -46.2% |

Brier score (lower is better) — model 0.2252 vs market 0.2014.

254 comparison(s) not yet settled and excluded rather than counted as losses.

**The claimed edge is anti-predictive.** Results get WORSE as the edge threshold rises, so the model's disagreement with the price measures the model's error, not the market's. The market is the better calibrated forecaster. Do not trade this.
