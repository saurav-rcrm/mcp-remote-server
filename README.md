# RecruitCRM MCP Server

This project is an MCP (Model-Controlled Program) server for interacting with the RecruitCRM API. It can be run locally for development or deployed as a remote web service to be used with any LLM platform.

## Local Development

To run the server on your local machine for testing and development:

1.  **Create a virtual environment:**
    ```bash
    python -m venv venv
    source venv/bin/activate
    ```

2.  **Install dependencies:**
    ```bash
    pip install -r requirements.txt
    ```

3.  **Set up your environment variables:**
    Create a file named `.env` in the project root and add your RecruitCRM API token:
    ```
    RCRM_TOKEN="your-api-token-here"
    ```

4.  **Run the server:**
    ```bash
    python recruitcrm_mcp.py
    ```
    The server will start on `http://localhost:8000`.

## Deployment to Render

This application is configured for production deployment on [Render](https://render.com/) using Gunicorn.

1.  **Push your code to GitHub:**
    Create a new repository on GitHub and push this project's code.

2.  **Create a New Web Service on Render:**
    - Go to your Render Dashboard and click **New +** > **Web Service**.
    - Connect your GitHub account and select your repository.

3.  **Configure the Service:**
    - **Name:** A name for your service (e.g., `recruitcrm-mcp-server`).
    - **Runtime:** Render should automatically detect `Python 3`.
    - **Build Command:** `pip install -r requirements.txt`
    - **Start Command:** Render should automatically use the `web` process from the `Procfile`. The command is `gunicorn -w 4 -k uvicorn.workers.UvicornWorker --bind 0.0.0.0:$PORT recruitcrm_mcp:app`. You should not need to change this.

4.  **Add Environment Variables:**
    - Go to the **Environment** tab for your new service.
    - Add a Secret File:
        - **Filename:** `.env`
        - **Contents:** `RCRM_TOKEN="your-api-token-here"`
    - This keeps your API token secure.

5.  **Deploy:**
    - Click **Create Web Service**.
    - Render will build and deploy your application. Once complete, you will get a public URL (e.g., `https://your-app-name.onrender.com`).

## Using the Remote MCP Server

Once deployed, you can use your MCP server as a custom tool in any LLM platform that supports them (like OpenAI's Assistants API).

Use the following configuration:

-   **URL:** `https://your-app-name.onrender.com/mcp`
-   **Transport:** `sse` (Server-Sent Events) 