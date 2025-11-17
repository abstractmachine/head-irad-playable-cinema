a# Playable Local
We are going to explore local inferencing. We'll start by using [Ollama](https://ollama.com/) which allows us to easily run various open source models on an Apple Silicon Mac.

## Installation
Here were the basic installation steps we used for testing, mostly using Ollama for its easy installation.

1. Create an account on [Ollama](https://ollama.com/) and [Download Ollama](https://ollama.com/download) for your platform.
2. `Ollama` > `Settings` > Connect account
3. Note: Ollama will run in background. Cf. `System Settings` > `General` > `Ollama`
4. Install Model. For example, to load `llama 3.2 Vision`: `$ ollama pull llama3.2-vision`
5. Open Ollama via app where you can easily do drag/drop image tests, or from the command line: `$ ollama run llama3.2-vision` and then create a python or web app to test the inferencing.

## JSON Formatted Results
Similar to the OpenAI API, we can get JSON-formatted results from our Gemma3 queries. Cf. [A Practical Guide: Getting Structured JSON from Gemma 3 and Ollama](https://www.linkedin.com/pulse/structured-output-gemma3-ali-afshar-nadae) & [Structured Outputs](https://ollama.com/blog/structured-outputs) from the Ollama Blog.

## Tests
Here are the outputs of the various models tests, more or less in order of quality.

1. [Gemma3:27B](./tests.md#gemma3-27b)
2. [Gemma3:12B](./tests.md#gemma3-12b)
3. [Gemma3:4B](./tests.md#gemma3-4b)
4. [Llama 3.2 Vision](./tests.md#llama-32-vision)
5. Llava 7b
5. mistral-small3.2

Failed:
1. [OpenAI OSS](./tests.md#gpt-oss)
2. [Llama 4 Scout](./tests.md#llama-4-scout)