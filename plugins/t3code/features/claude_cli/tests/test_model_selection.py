"""Account-resolved identities and context launch policy; no model calls."""
import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('claude_selection_test', ROOT/'model_selection.py')
selection = importlib.util.module_from_spec(spec)
spec.loader.exec_module(selection)


import unittest

class ModelSelectionTests(unittest.TestCase):
    def test_catalog_keeps_account_versions_and_never_invents_latest_releases(self):
        rows = selection.model_rows([
            {'value':'default','resolvedModel':'claude-opus-5-5','displayName':'Default'},
            {'value':'opus','resolvedModel':'claude-opus-5-5','displayName':'Opus 5.5','supportsEffort':True},
            {'value':'sonnet','resolvedModel':'claude-sonnet-4-6','displayName':'Sonnet 4.6'},
            {'value':'custom-model','displayName':'Custom'},
        ])
        assert [r['id'] for r in rows] == ['claude-opus-5-5','claude-opus-5-5[200k]','claude-sonnet-4-6','custom-model']
        assert rows[0]['label'] == 'Opus 5.5'
        assert rows[0]['aliases'] == ['default','opus']
        assert rows[0]['context_window'] == 1_000_000
        assert rows[1]['context_window'] == 200_000
        assert 'claude-sonnet-5-5' not in {r['id'] for r in rows}
        assert selection.ModelCapabilities().get('claude-sonnet-5-5[200k]')['context_window'] == 200_000


    def test_context_selection_is_translated_to_native_launch_not_sent_as_a_fake_model(self):
        spec = importlib.util.spec_from_file_location('claude_options_test', ROOT/'__init__.py', submodule_search_locations=[str(ROOT)])
        package = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = package
        spec.loader.exec_module(package)
        from claude_options_test.options import native_options
        env = {'PATH':'/usr/bin:/bin','CLAUDE_CODE_DISABLE_1M_CONTEXT':'1'}
        state = {'session_id':'native-session','config_directory':''}
        capped = native_options({}, env, '/workspace', state, model='claude-sonnet-5-5[200k]')
        assert capped.model == 'claude-sonnet-5-5'
        assert capped.resume == 'native-session'
        assert capped.env['CLAUDE_CODE_DISABLE_1M_CONTEXT'] == '1'
        assert capped.env['CLAUDE_CODE_AUTO_COMPACT_WINDOW'] == '200000'
        large = native_options({}, env, '/workspace', state, model='claude-sonnet-5-5[1m]')
        assert large.model == 'claude-sonnet-5-5'
        assert large.env['CLAUDE_CODE_DISABLE_1M_CONTEXT'] == ''
        assert large.env['CLAUDE_CODE_AUTO_COMPACT_WINDOW'] == '1000000'
        assert env == {'PATH':'/usr/bin:/bin','CLAUDE_CODE_DISABLE_1M_CONTEXT':'1'}
        probe = native_options({}, env, '/workspace', {}, probe=True)
        assert probe.model is None
        assert probe.extra_args['no-session-persistence'] is None

    def test_context_contract_survives_cached_helper_from_before_update(self):
        from unittest.mock import patch
        import importlib
        spec = importlib.util.spec_from_file_location('claude_cached_helper_test', ROOT/'__init__.py', submodule_search_locations=[str(ROOT)])
        package = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = package
        spec.loader.exec_module(package)
        options = importlib.import_module('claude_cached_helper_test.options')
        helper = importlib.import_module('claude_cached_helper_test.model_selection')
        current_policy = helper.context_policy

        # An already-running backend can hold the original required-settings helper
        # while lazily importing the updated options/runtime for the first Claude turn.
        def context_policy(model, settings):
            return helper.split_model(model)[1]

        with patch.object(helper, 'context_policy', context_policy):
            launched = options.native_options({}, {}, '/workspace', {}, model='claude-opus-5-5')
            assert launched.model == 'claude-opus-5-5'
        # Old callers must also remain valid against a freshly imported helper.
        assert current_policy('claude-opus-5-5[200k]', {}) == '200k'

