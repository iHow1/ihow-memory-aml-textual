# Third-party notices

This repository vendors no third-party code. The service installs the
following package from PyPI at deploy time (`requirements.txt`).

## tiktoken 0.12.0

- Source: https://github.com/openai/tiktoken
- License: MIT (license text below, copied from the 0.12.0 wheel)
- Use: counting tokens with the `o200k_base` encoding. The encoding file is
  downloaded by tiktoken from OpenAI's public host on first use and is not
  included here.

Its own dependencies are installed by pip and are not redistributed here:
`regex` (Apache-2.0 AND CNRI-Python), `requests` (Apache-2.0), `urllib3`
(MIT), `idna` (BSD-3-Clause), `certifi` (MPL-2.0), `charset-normalizer` (MIT).
The versions pip resolves may differ from the ones checked when this file was
written.

```
MIT License

Copyright (c) 2022 OpenAI, Shantanu Jain

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
```
