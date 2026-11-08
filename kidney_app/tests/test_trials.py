"""Pregnancy-exclusion flag: the cases found while checking the real California export."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ingest.clinicaltrials import _excludes_pregnancy as f  # noqa: E402

CASES = {
    "Inclusion Criteria: * Males and females (not pregnant)\nExclusion Criteria: * dialysis": True,
    "Inclusion: adults\nExclusion Criteria: pregnant or breastfeeding women": True,
    "Inclusion Criteria: negative urine pregnancy test at screening\nExclusion Criteria: none": True,
    "Inclusion Criteria: use contraception; no sexual relations which carry a risk of pregnancy\nExclusion Criteria: x": True,
    "Inclusion Criteria: * pregnant women 18+ with CKD\nExclusion Criteria: * dialysis": False,
    "Inclusion Criteria: * singleton pregnancy at 12-20 weeks of gestation\nExclusion Criteria: * none": False,
    "Inclusion Criteria: eGFR > 30\nExclusion Criteria: active infection": None,
    "": None,
}


def test_three_valued_pregnancy_flag():
    for text, want in CASES.items():
        assert f(text) is want, (want, text)


if __name__ == "__main__":
    test_three_valued_pregnancy_flag()
    print("ok test_three_valued_pregnancy_flag")
