# Architecture reference and dependencies

The implementation was informed by T3 Code's Claude adapter architecture, inspected at commit `a9fb6a80635c77a1d4fd988e0018059e13628eff` on 2026-10-07:

- https://github.com/pingdotgg/t3code/blob/a9fb6a80635c77a1d4fd988e0018059e13628eff/apps/server/src/orchestration-v2/Adapters/ClaudeAdapterV2.ts
- https://github.com/pingdotgg/t3code/blob/a9fb6a80635c77a1d4fd988e0018059e13628eff/apps/server/src/provider/ClaudeProvider.ts

This is an independent Python adapter, not a drop-in copy of T3's TypeScript implementation. T3's license is reproduced below for attribution.

The Claude Agent SDK and installed Claude Code executable retain their own upstream licenses and terms. They are dependencies, not vendored into this plugin. SDK documentation: https://platform.claude.com/docs/en/agent-sdk/python

## T3 Code MIT license

Copyright (c) 2026 T3 Tools Inc.

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
