from abc import ABC, abstractmethod
from langchain_core.language_models.chat_models import BaseChatModel
from langgraph.graph import StateGraph, END, START
from typing import TypedDict, Annotated, Sequence, List
from langgraph.graph.message import add_messages
from langchain_core.messages import BaseMessage, ToolMessage, AIMessage, SystemMessage, HumanMessage
from .tools import read_file, get_contents_of_directory
from langgraph.graph.state import CompiledStateGraph
from pathlib import Path
import re
from langgraph.prebuilt import ToolNode
from rich.console import Console
from rich.text import Text

class Agent(ABC):
    """Abstract base class for all agents."""
    
    def invoke(self, message : HumanMessage) -> dict:
        """Invoke the agent, print the stream and return the state"""
        s = self.__print_stream(self.agent.stream({"messages":message}, stream_mode="values", config={
            "configurable": {
                "recursion_limit": 50
            }
        }))
        return s
    
    @abstractmethod
    def ___construct_and_compile_graph__(self) -> CompiledStateGraph:
        pass 
    
    def show_graph_diagram(self, compiled_graph : CompiledStateGraph) -> None:
        """
        Print agent graph using ASCII

        Args:
            compiled_graph (CompiledStateGraph): compiled graph, ___construct_and_compile_graph__ should return CompiledStateGraph
        """
        print(compiled_graph.get_graph().draw_ascii())
        
    def load_prompt(self, agent_type: str, prompt_type : str, prompt_name: str) -> str:
        """
        Load a prompt from the prompts directory.
        
        Args:
            prompt_type (str): The type of prompt (e.g., 'react')
            prompt_name (str): The name of the prompt file (e.g., 'react_system_prompt.txt')
        
        Returns:
            str: The content of the prompt file
        
        Raises:
            FileNotFoundError: If the prompt file cannot be found
        """
        # Get the project root directory (two levels up from agentic/agents.py)
        project_root = Path(__file__).parent.parent
        prompt_path = project_root / "prompts" / agent_type / prompt_type / f"{prompt_name}.txt"
        
        # Read the prompt from file
        with open(prompt_path, 'r') as f:
            return f.read().strip()
    
        
    def extract_label_confidence(self, text: str) -> dict:
        """
        Extracts label and confidence from a formatted string like:

        @@LABEL: Web.Backend@@
        @@CONFIDENCE: 0.5@@

        Returns:
            dict: {"label": list[label], "confidence": list[confidence]}
        """
        label_match = re.findall(r"@@LABEL:\s*([^@]+)@@", text)
        confidence_match = re.findall(r"@@CONFIDENCE:\s*([\d.]+)@@", text)

        if not label_match or not confidence_match:
            raise ValueError("Input string is missing required LABEL or CONFIDENCE fields")

        label_match = list(label_match)
        confidence = list(confidence_match)

        return {"label": label_match, "confidence": confidence}

    
    def __print_stream(self, stream):
        # State for the current contiguous tail of ToolMessages
        tool_tail_printed = 0  # how many tool messages from the current tail we've printed
        console = Console()


        for s in stream:
            messages = s["messages"]
            if not messages:
                continue

            last = messages[-1]
            content = getattr(last, "content", "")

            # 1) Detect the length of the trailing run of ToolMessage objects.
            tail_len = 0
            i = len(messages) - 1
            while i >= 0 and isinstance(messages[i], ToolMessage):
                tail_len += 1
                i -= 1

            # 2) If there is a tool tail and it directly follows an AIMessage, print only the *new* tool outputs.
            if tail_len > 0 and i >= 0 and isinstance(messages[i], AIMessage):
                start = len(messages) - tail_len + tool_tail_printed
                # print any new tool messages in the tail
                for j in range(start, len(messages)):
                    tm = messages[j]
                    tm_content = getattr(tm, "content", "")
                    console.print(Text(f"====Tool Output====\n{tm_content}", style="#FF00FF"))
                # update how many of the current tail we have printed
                tool_tail_printed = tail_len
                continue
            else:
                # Not in a valid tool tail run; reset the counter.
                tool_tail_printed = 0

            # 3) Handle normal (non-tool-tail) cases.
            if isinstance(last, HumanMessage):
                console.print(Text(f"====Human Message====\n{content}", style="#00FFFF"))

            elif isinstance(last, AIMessage) and hasattr(last, "tool_calls") and last.tool_calls:
                for tc in last.tool_calls:
                    console.print(Text(
                        f"====Tool Call====\nTool Name: {tc.get('name')}\nTool Args: {tc.get('args')}",
                        style="purple"
                    ))

            elif isinstance(last, AIMessage):
                console.print(Text(f"====AI Message====\n{content}", style="#00FF00"))

            elif isinstance(last, SystemMessage):
                console.print(Text(f"====System Message====\n{content}", style="bright_green"))

            elif isinstance(last, ToolMessage):
                # Single, isolated ToolMessage not following an AI tail -> do not print (per your rule)
                pass

            else:
                console.print(Text(str(last), style="white"))
        return s


class ReactAgentState(TypedDict): #
        # AgentState responsible for keeping the state in the graph, inner class of ReActAgent
        messages : Annotated[Sequence[BaseMessage], add_messages] # Messages field so that the agent does not forget previous tool invocations etc.
        label : List[str]  # label that corresponds to the type of application
        confidence : List[float]  # field that corresponds to confidence, ranges from 0.0 to 1.0
        
class ReActAgent(Agent):

    def __init__(self, tool_calling_llm :  BaseChatModel, reasoning_llm : BaseChatModel) -> None:
        """Initialize the ReAct agent with an LLM instance."""
        self.tool_calling_llm = tool_calling_llm
        self.reasoning_llm = reasoning_llm
        self.___construct_and_compile_graph__()
    
    def ___construct_and_compile_graph__(self) -> CompiledStateGraph:
        """Constructs the state graph necessary for react agent
        Returns: StateGraph
        """
        def model_call(state : ReactAgentState) -> ReactAgentState:
            """calls tool calling LLM

            Args:
                state (ReactAgentState): graph state

            Returns:
                AgentState: updated graph state
            """
            # Load the system prompt using the utility function
            SYSTEM_PROMPT = self.load_prompt(agent_type="react", prompt_type="system", prompt_name="react_system_prompt")
            system_prompt = SystemMessage(content = SYSTEM_PROMPT)
            response = self.tool_calling_llm.invoke([system_prompt] + state["messages"])
            
            return {"messages" : [response]}
        
        def reason(state : ReactAgentState) -> ReactAgentState:
            """Calls reasoning LLM for analysis"""
            # Load the reasoning prompt using the utility function
            REASON_SYSTEM_PROMPT = self.load_prompt("react","system", "reasoning_prompt")
            system_prompt = SystemMessage(content = REASON_SYSTEM_PROMPT)
            response = self.reasoning_llm.invoke([system_prompt] + state["messages"])
            
            return {"messages" : [response]}
        
        def generate_label(state: ReactAgentState) -> ReactAgentState:
            """Generates labels based on previous inferences of the agent

            Args:
                state (ReactAgentState): typed dict agent state

            Returns:
                AgentState: initialized fields "label" and "confidence"
            """
            system_prompt = SystemMessage(content=self.load_prompt("react", "system", "react_system_prompt"))
            label_generation_prompt = HumanMessage(content= self.load_prompt("react", "user", "label_generation_prompt"))

            response = self.reasoning_llm.invoke(
                [system_prompt] + state["messages"] + [label_generation_prompt]
            )

            # your helper
            label_and_confidence = self.extract_label_confidence(response.content)

            return {
                "messages": state["messages"] + [label_generation_prompt, response],
                "label": label_and_confidence["label"],
                "confidence": label_and_confidence["confidence"],
            }
        
        def should_continue(state : ReactAgentState) -> str:
            """Function for the conditional edge, checks if LLM requested to call any tools and if not signals the end of the loop

            Args:
                state (AgentState): graph state

            Returns:
                AgentState: updated graph state
            """
            last_message = state["messages"][-1]
            
            if not last_message.tool_calls:
                return "end"
            else:
                return "continue"
        
        tools = [read_file, get_contents_of_directory]
        self.tool_calling_llm = self.tool_calling_llm.bind_tools(tools)
        
        tool_node = ToolNode(tools=tools)
        
        graph = StateGraph(ReactAgentState)
        graph.add_node("reason", reason)
        graph.add_node("our_agent", model_call)
        graph.add_node("label_generation", generate_label)
        graph.add_node("tools", tool_node)
        
        graph.add_edge(START, "our_agent")
        graph.add_edge("tools", "reason")
        graph.add_edge("reason", "our_agent")
        graph.add_conditional_edges(
            "our_agent",
            should_continue,
            {
                "continue": "tools",
                "end": "label_generation",
            },
        )
        graph.add_edge("label_generation", END)
        self.agent = graph.compile()
        
        return self.agent 


                
    