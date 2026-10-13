"""Native display projection and callback translation; no inference or account access."""
import asyncio
import importlib.util
import json
import queue
import sys
import unittest
from pathlib import Path

root = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('claude_cli_plugin', root/'__init__.py', submodule_search_locations=[str(root)])
plugin = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = plugin
spec.loader.exec_module(plugin)
from claude_cli_plugin.projection import Projector
from claude_cli_plugin.sdk_runtime import ClaudeRuntime, _prompt
from claude_agent_sdk import AssistantMessage, UserMessage, TextBlock, ToolUseBlock, ToolResultBlock, SystemMessage
from claude_agent_sdk.types import StreamEvent


class ProjectionTests(unittest.TestCase):
    def test_native_tool_identity_and_exact_input_survive_display_projection(self):
        p = Projector('/workspace', '')
        inputs = {'command': 'printf hello', 'timeout': 5000}
        rows = p.project(AssistantMessage(content=[TextBlock('Running'), ToolUseBlock('t1','Bash',inputs)], model='test',uuid='m1'))
        self.assertEqual(json.loads(rows[0]['tool_calls'][0]['function']['arguments']), inputs)
        self.assertEqual(rows[1]['id'], 't1')
        self.assertEqual(p.project(AssistantMessage(content=[TextBlock('Running')], model='test',uuid='m1')), [])
        result = p.project(UserMessage(content=[ToolResultBlock('t1','hello',False)]))[0]
        self.assertEqual((result['id'],result['name'],result['content']),('t1','Bash','hello'))
        self.assertEqual(result['input'],inputs)

    def test_subagent_text_does_not_pollute_root_answer(self):
        p = Projector('/workspace','')
        self.assertEqual(p.project(StreamEvent(uuid='s1',session_id='native1',parent_tool_use_id='sub1',event={'delta':{'type':'text_delta','text':'child'}})),[])
        self.assertEqual(p.project(AssistantMessage(content=[TextBlock('child')],model='test',parent_tool_use_id='sub1')),[])
        self.assertEqual(p.project(StreamEvent(uuid='s2',session_id='native1',event={'delta':{'type':'text_delta','text':'root'}}))[0]['text'],'root')

    def test_session_and_compaction_are_native_state_and_status(self):
        p=Projector('/workspace','/native-config')
        state=p.project(SystemMessage(subtype='init',data={'session_id':'native1'}))[0]['state']
        self.assertEqual(state,{'session_id':'native1','cwd':'/workspace','config_directory':'/native-config'})
        self.assertEqual(p.project(SystemMessage(subtype='compact_boundary',data={}))[0]['type'],'status')

    def test_remote_image_is_rejected_before_query(self):
        with self.assertRaises(ValueError):
            _prompt([{'type':'image_url','image_url':{'url':'https://example.com/image.png'}}])


class QuestionTests(unittest.IsolatedAsyncioTestCase):
    async def test_question_callback_updates_native_answers_and_preserves_inputs(self):
        rt=ClaudeRuntime.__new__(ClaudeRuntime)
        rt._closed=False; rt._interrupt_pending=__import__('threading').Event()
        rt._pending={}; rt._active_events=queue.Queue(); rt._loop=asyncio.get_running_loop()
        inputs={'questions':[{'question':'Which?', 'options':[{'label':'A'},{'label':'B'}]}]}
        task=asyncio.create_task(rt._permission('AskUserQuestion',inputs,None))
        await asyncio.sleep(0)
        event=rt._active_events.get_nowait()
        self.assertEqual(event['questions'][0]['choices'],['A','B'])
        rt.respond(event['id'],{'allowed':True,'answer':{'answers':{'q0':'B'}}})
        result=await task
        self.assertEqual(result.updated_input['answers'],{'Which?':'B'})
        self.assertNotIn('answers',inputs)

    async def test_stopping_before_a_tool_request_denies_it(self):
        rt=ClaudeRuntime.__new__(ClaudeRuntime)
        rt._closed=True; rt._interrupt_pending=__import__('threading').Event()
        result=await rt._permission('Bash',{'command':'echo test'},None)
        self.assertEqual(result.behavior,'deny')
        self.assertTrue(result.interrupt)


if __name__=='__main__':
    unittest.main()
