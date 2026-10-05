"""Rolling diarization, light half: local clusters mapped onto global speakers by centroid cosine, and relabelling of the
final groups by majority overlap. The sherpa-onnx half (local_clusters) needs the models and is not run here."""
import numpy as np

from diar_rolling import ChunkDiarizer


def unit(*v):
    v = np.array(v, np.float32); return v / np.linalg.norm(v)


def test_assign_new_then_matching_speakers():
    d = ChunkDiarizer(match_threshold=0.45)
    out, gmap = d.assign({0: [(0, 10)], 1: [(10, 12)]}, {0: unit(1, 0), 1: unit(0, 1)}, 600)
    assert gmap == {0: 0, 1: 1} and out == [(600, 610, "Speaker 1"), (610, 612, "Speaker 2")]
    out, gmap = d.assign({5: [(0, 3)], 6: [(3, 9)]}, {5: unit(0.1, 1), 6: unit(1, 0.05)}, 1200)
    assert gmap == {6: 0, 5: 1} and len(d.centroids) == 2                 # the next chunk's clusters find the same people


def test_cluster_without_embedding_gets_no_label():
    d = ChunkDiarizer()
    out, gmap = d.assign({0: [(0, 0.5)]}, {0: None}, 0)
    assert out == [] and gmap == {0: None} and d.centroids == []


def test_known_speaker_count_caps_new_speakers():
    d = ChunkDiarizer(match_threshold=0.99, num_speakers=1)
    d.assign({0: [(0, 5)]}, {0: unit(1, 0)}, 0)
    _, gmap = d.assign({0: [(0, 5)]}, {0: unit(0, 1)}, 600)                  # far away, but only one participant
    assert gmap == {0: 0} and len(d.centroids) == 1


def test_relabel_majority_overlap():
    finals = [{"s": 0, "e": 10, "speaker": "Speaker 9"}, {"s": 10, "e": 12, "speaker": "Speaker 2"}, {"s": 50, "e": 60, "speaker": "x"}]
    dsegs = [(0, 3, "Speaker 1"), (3, 11, "Speaker 2")]
    assert ChunkDiarizer.relabel(finals, dsegs) == 1
    assert [f["speaker"] for f in finals] == ["Speaker 2", "Speaker 2", "x"] and finals[0]["speaker_online"] == "Speaker 9"
