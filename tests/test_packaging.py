"""The same network-free check runs inside the actual Windows executable."""
import json

from main import self_test


def test_packaged_self_test_exercises_launchbox_catalogue_and_source_chain(tmp_path):
    output = tmp_path/'result.json'
    assert self_test(output)
    result = json.loads(output.read_text(encoding='utf-8'))
    assert result['success'] is True
    assert 'LaunchBox catalogue parser' in result['tests']
    assert 'automatic source fallback' in result['tests']
    assert 'coverage matching' in result['tests']
    assert 'provider adapters' in result['tests']
    assert 'result report' in result['tests']
