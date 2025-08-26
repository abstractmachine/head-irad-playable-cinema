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