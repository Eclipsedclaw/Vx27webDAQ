// Signal processing for the scope tools of the waveform panel (WaveDump2's plot types): the FFT of a waveform,
// averaged over the newest N events, and the histogram of its sample values, filled event after event.

export function nextPow2(n) {
  let p = 1;
  while (p < n) p <<= 1;
  return p;
}

// in place, iterative radix-2 (n a power of 2)
function fft(re, im) {
  const n = re.length;
  for (let i = 1, j = 0; i < n; i++) {
    let bit = n >> 1;
    for (; j & bit; bit >>= 1) j ^= bit;
    j ^= bit;
    if (i < j) {
      let x = re[i]; re[i] = re[j]; re[j] = x;
      x = im[i]; im[i] = im[j]; im[j] = x;
    }
  }
  for (let len = 2; len <= n; len <<= 1) {
    const ang = (-2 * Math.PI) / len, wr = Math.cos(ang), wi = Math.sin(ang), half = len >> 1;
    for (let i = 0; i < n; i += len) {
      let cr = 1, ci = 0;
      for (let k = 0; k < half; k++) {
        const a = i + k, b = a + half;
        const tr = re[b] * cr - im[b] * ci, ti = re[b] * ci + im[b] * cr;
        re[b] = re[a] - tr; im[b] = im[a] - ti;
        re[a] += tr; im[a] += ti;
        const nr = cr * wr - ci * wi;
        ci = cr * wi + ci * wr;
        cr = nr;
      }
    }
  }
}

// |X_k|^2 of one waveform, k = 0 … N/2: the mean taken off, a Hann window, zeros up to a power of 2 (N)
export function powerSpectrum(samples) {
  const m = samples.length, n = nextPow2(Math.max(2, m));
  let mean = 0;
  for (let i = 0; i < m; i++) mean += samples[i];
  mean /= m;
  const re = new Float64Array(n), im = new Float64Array(n);
  for (let i = 0; i < m; i++) re[i] = (samples[i] - mean) * (0.5 - 0.5 * Math.cos((2 * Math.PI * i) / Math.max(1, m - 1)));
  fft(re, im);
  const p = new Float64Array(n / 2 + 1);
  for (let k = 0; k <= n / 2; k++) p[k] = re[k] * re[k] + im[k] * im[k];
  return { power: p, n, m };
}

// summed power of `count` events -> frequency (MHz) and amplitude in dB of full scale (a sine filling the
// ADC range is 0 dBFS); the Hann window's sum (m/2) is divided out
export function spectrumDb(power, count, n, m, sampleNs, adcMax) {
  const freq = new Float64Array(power.length), db = new Float64Array(power.length);
  const norm = 2 / (m / 2), full = (adcMax + 1) / 2;
  for (let k = 0; k < power.length; k++) {
    freq[k] = (k / (n * sampleNs)) * 1000;
    db[k] = 20 * Math.log10(Math.max((Math.sqrt(power[k] / count) * norm) / full, 1e-12));
  }
  return { freq, db };
}

// the newest `size` arrays of the same length, and their sum (for an average)
export class Ring {
  constructor(size) { this.size = size; this.items = []; this.sum = null; }
  add(arr) {
    if (this.sum && this.sum.length !== arr.length) this.clear();
    if (!this.sum) this.sum = new Float64Array(arr.length);
    for (let i = 0; i < arr.length; i++) this.sum[i] += arr[i];
    this.items.push(arr);
    while (this.items.length > this.size) {
      const old = this.items.shift();
      for (let i = 0; i < old.length; i++) this.sum[i] -= old[i];
    }
  }
  mean() {
    const n = this.items.length, out = new Float64Array(this.sum ? this.sum.length : 0);
    for (let i = 0; i < out.length; i++) out[i] = this.sum[i] / n;
    return out;
  }
  get count() { return this.items.length; }
  clear() { this.items = []; this.sum = null; }
}

// the sample values of many events: counts per ADC code; bins() joins codes so at most `most` bins remain
export class SampleHistogram {
  constructor() { this.counts = new Map(); this.n = 0; }
  add(samples) {
    for (let i = 0; i < samples.length; i++) {
      const v = Math.round(samples[i]);
      this.counts.set(v, (this.counts.get(v) || 0) + 1);
    }
    this.n += samples.length;
  }
  clear() { this.counts = new Map(); this.n = 0; }
  stats() {
    let s = 0, s2 = 0;
    for (const [v, c] of this.counts) { s += v * c; s2 += v * v * c; }
    const mean = this.n ? s / this.n : 0;
    return { n: this.n, mean, std: this.n ? Math.sqrt(Math.max(0, s2 / this.n - mean * mean)) : 0 };
  }
  bins(most = 400) {
    if (!this.counts.size) return { xs: [], ys: [], width: 1 };
    const keys = [...this.counts.keys()];
    const lo = Math.min(...keys), hi = Math.max(...keys);
    const width = Math.max(1, Math.ceil((hi - lo + 1) / most));
    const nb = Math.floor((hi - lo) / width) + 1, ys = new Array(nb).fill(0), xs = new Array(nb);
    for (const [v, c] of this.counts) ys[Math.floor((v - lo) / width)] += c;
    for (let b = 0; b < nb; b++) xs[b] = lo + b * width;
    return { xs, ys, width };
  }
}
