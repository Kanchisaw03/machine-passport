import pytest
from scripts.verify_gemini import has_credentials, verify

pytestmark=[pytest.mark.live,pytest.mark.skipif(not has_credentials(),reason="Gemini credentials are not configured")]

def test_live_gemini_voice_retrieval_recommendation_and_grounding():
    results=verify(verbose=False)
    assert results and all(status=="PASS" for _,status,_ in results), results
