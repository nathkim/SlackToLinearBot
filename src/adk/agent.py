import os, json, requests, uuid
from google.adk.agents import SequentialAgent, LlmAgent, BaseAgent
from .slack_tools import post_approval_message
from .linear_tools import get_issues, compare, input_for_slack, callback, update_linear_priority, match_issue, handle_dm_update, list_linear_issues, handle_metrics

ADK_BASE_URL = "https://adk-service-668646793196.us-central1.run.app"
ROUTING_AGENT_NAME = "adk"

''' # Use if MCP can be authenticated in ADK
from google.adk.tools.mcp_tool.mcp_toolset import (
    MCPToolset,
    StdioConnectionParams,
    StdioServerParameters,
    AuthParameters,
    OAuth2AuthParameters
)


linear_toolset = MCPToolset(
    connection_params=StdioConnectionParams(
        server_params=StdioServerParameters(
            command="npx",
            args=[
                "-y",  # Argument for npx to auto-confirm install
                "mcp-remote",
                "https://mcp.linear.app/sse",
            ],
            env={
                "LINEAR_API_KEY": os.getenv("LINEAR_API_KEY", ""),
            },
        ),
        timeout=60 * 20,
    )
)
'''

# --- Linear Agent ---
linear_agent = LlmAgent(
    name = "LinearAgent",
    model = "gemini-2.0-flash-lite",
    description = "Agent that compares tasks to Linear issues and updates as needed.",
    instruction = (
        "You are given a task from a team standup and the company's Linear workspace."
        "Your job is to pull the list of current issues fetched from Linear using get_issues tool."
        "Compare these issues with the given task using the compare tool."
        "Use the comparison data and call the input_for_slack tool."
    ),
    tools = [get_issues, compare, input_for_slack]
)

# --- Linear MCP Agent ---
linear_dm_agent = LlmAgent(
    name="LinearDMAgent",
    model = "gemini-2.0-flash-lite",
    description="Agent that takes slack DM and chooses the right tool to call to Linear",
    instruction=(
        "You are given natural language question or command regarding data in Linear."
        "Your job is to determine which tool best fits this message and respond to the best of your ability."
        "Use the list_linear_issues tool if the message is asking for issues in Linear"
        "Use update_linear_priority tool if the message is asking to change a task's priority by first calling match_issue. Use context from the message to match to a number from: 0= No priority, 1= Urgent, 2= High, 3= Medium, 4= Low. Next, send the parameter in the form task_data = {'title': '<output of match_issue>,'priority': <number 0-4>}"
        "Use the handle_dm_update tool if given a query that mentions a status change like 'In Progress', 'In Review', 'Done', etc"
        "Use the handle_metrics tool for all other questions regarding metrics. These include but are not limited to number of tasks in different statuses, any data filtered by name, average time for tasks, number of tasks in different statuses, information of tasks done by certain names etc. Be sure to include all relevant details in the output." 
        "Do not include any info regarding your workflow like 'I will transfer you to <AdkAgent>' in any of the outputs"
    ),
    tools = [get_issues, match_issue, update_linear_priority, handle_dm_update, list_linear_issues, handle_metrics]
)

# --- Slack Agent ---
slack_agent = LlmAgent(
    name="SlackAgent",
    model="gemini-2.0-flash-lite",
    description="Agent that verifies and notifies users based on changes in Linear.",
    instruction=(
        "You are given a Linear task's expected and current status which needs an update."
        "Your job is to notify these differences in Slack using post_approval_message."
        "When a reaction is given and handle_reaction_added is used, post what happens"
    ),
    tools = [post_approval_message]
)

# --- Orchestrator ---
root_agent = LlmAgent(
    name="OrchestratorAgent",
    model = "gemini-2.0-flash-lite",
    sub_agents = [linear_agent, slack_agent, linear_dm_agent],
    description=(
        "You are a routing agent for SZNS. Based on the user input, decide which of the following agents to invoke:\n"
        "- LinearAgent: to compare and update standup tasks to Linear issues.\n"
        "- SlackAgent: to notify users and request approval in Slack.\n"
        "- LinearDMAgent: to answer natural language questions about the Linear workspace.\n"
        "Always select the most relevant agent to handle the request."
    ),
    instruction=(
        "Analyze the user's request and choose the most appropriate agent. "
        "Use LinearDMAgent for natural language Linear queries or commands like 'What issues are assigned to me?' or 'Change task to low'."
        "Only use LinearAgent if given a JSON with the 'name', 'task', and 'status' keys to compare to current Linear issues"
        "Only use SlackAgent AFTER using LinearAgent."
    )
    # tools=[post_approval_message, linear_toolset, get_issues, compare, input_for_slack]
)