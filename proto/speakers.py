"""Online speaker labelling with running centroids (design 1.5a step 1).

Each known speaker keeps the normalised mean of all embeddings assigned so far; a new segment is compared
(cosine) with the centroids, not with a single first segment. This matches how the threshold was calibrated
(cosine of a segment to the centroid of long segments, report 1.4).
"""
import numpy as np


class CentroidSpeakers:
    def __init__(self, threshold=0.45, prefix="Speaker"):
        self.threshold, self.prefix = threshold, prefix
        self.sums, self.counts, self.names = [], [], []

    @staticmethod
    def _unit(e):
        e = np.asarray(e, dtype=np.float32)
        return e / (np.linalg.norm(e) + 1e-9)

    def centroids(self):
        return [self._unit(s) for s in self.sums]

    def label(self, e):
        """Returns the label for embedding e, creating a new speaker when no centroid is close enough."""
        e = self._unit(e)
        if self.sums:
            sims = [float(c @ e) for c in self.centroids()]
            k = int(np.argmax(sims))
            if sims[k] >= self.threshold:
                self.sums[k] += e; self.counts[k] += 1
                return self.names[k]
        self.sums.append(e.copy()); self.counts.append(1); self.names.append(f"{self.prefix} {len(self.names) + 1}")
        return self.names[-1]

    def __len__(self):
        return len(self.names)


if __name__ == "__main__":  # self-check
    rng = np.random.default_rng(0)
    a, b = rng.normal(size=64), rng.normal(size=64)
    m = CentroidSpeakers(threshold=0.45)
    assert m.label(a) == "Speaker 1" and m.label(a + 0.3 * rng.normal(size=64)) == "Speaker 1"
    assert m.label(b) == "Speaker 2" and len(m) == 2
    assert m.label(b + 0.3 * rng.normal(size=64)) == "Speaker 2" and m.counts == [2, 2]
    print("speakers self-check OK")
