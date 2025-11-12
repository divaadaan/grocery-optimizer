"""
DSPy 3.0 configuration for the grocery optimizer multi-agent system.

This module configures DSPy to work with local Ollama models, matching
the existing model setup (SmolLM variants).
"""

import dspy
import os
from typing import Optional


class DSPyConfig:
    """Configuration manager for DSPy language models."""

    # Model configurations matching the existing setup
    CHEF_MODEL = "smollm:1.7b"
    SOUS_CHEF_MODEL = "smollm:360m"
    NUTRITIONIST_MODEL = "smollm:360m"

    # Temperature settings per agent type
    CHEF_TEMPERATURE = 0.7
    SOUS_CHEF_TEMPERATURE = 0.8
    NUTRITIONIST_TEMPERATURE = 0.3

    # Ollama base URL (default)
    OLLAMA_BASE_URL = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")

    @classmethod
    def configure_chef_lm(cls) -> dspy.LM:
        """
        Configure language model for Chef Orchestrator.

        Returns:
            Configured DSPy LM instance for Chef
        """
        lm = dspy.LM(
            model=f"ollama/{cls.CHEF_MODEL}",
            api_base=cls.OLLAMA_BASE_URL,
            temperature=cls.CHEF_TEMPERATURE,
            format="json",  # Request JSON format responses
        )
        return lm

    @classmethod
    def configure_sous_chef_lm(cls) -> dspy.LM:
        """
        Configure language model for Sous Chef.

        Returns:
            Configured DSPy LM instance for Sous Chef
        """
        lm = dspy.LM(
            model=f"ollama/{cls.SOUS_CHEF_MODEL}",
            api_base=cls.OLLAMA_BASE_URL,
            temperature=cls.SOUS_CHEF_TEMPERATURE,
            format="json",
        )
        return lm

    @classmethod
    def configure_nutritionist_lm(cls) -> dspy.LM:
        """
        Configure language model for Nutritionist.

        Returns:
            Configured DSPy LM instance for Nutritionist
        """
        lm = dspy.LM(
            model=f"ollama/{cls.NUTRITIONIST_MODEL}",
            api_base=cls.OLLAMA_BASE_URL,
            temperature=cls.NUTRITIONIST_TEMPERATURE,
            format="json",
        )
        return lm

    @classmethod
    def setup_all_agents(cls):
        """
        Configure DSPy with appropriate language models for all agents.

        This sets up default models and can be called at application startup.
        Individual agents can override with their specific LM.
        """
        # Set default LM for DSPy (using Chef model as default)
        chef_lm = cls.configure_chef_lm()
        dspy.settings.configure(lm=chef_lm)

        print(f"[DSPy] Configured with Ollama at {cls.OLLAMA_BASE_URL}")
        print(f"[DSPy] Chef model: {cls.CHEF_MODEL}")
        print(f"[DSPy] Sous Chef model: {cls.SOUS_CHEF_MODEL}")
        print(f"[DSPy] Nutritionist model: {cls.NUTRITIONIST_MODEL}")

        return {
            "chef": chef_lm,
            "sous_chef": cls.configure_sous_chef_lm(),
            "nutritionist": cls.configure_nutritionist_lm(),
        }


def initialize_dspy(agent_type: Optional[str] = None):
    """
    Initialize DSPy with appropriate language model configuration.

    Args:
        agent_type: Optional agent type ('chef', 'sous_chef', 'nutritionist')
                   If None, sets up all agents and uses chef as default

    Returns:
        Configured LM instance(s)
    """
    if agent_type == "chef":
        lm = DSPyConfig.configure_chef_lm()
        dspy.settings.configure(lm=lm)
        return lm
    elif agent_type == "sous_chef":
        lm = DSPyConfig.configure_sous_chef_lm()
        dspy.settings.configure(lm=lm)
        return lm
    elif agent_type == "nutritionist":
        lm = DSPyConfig.configure_nutritionist_lm()
        dspy.settings.configure(lm=lm)
        return lm
    else:
        # Setup all agents
        return DSPyConfig.setup_all_agents()


# Example usage:
if __name__ == "__main__":
    # Configure DSPy for all agents
    lms = initialize_dspy()

    # Test with a simple signature
    class SimpleQA(dspy.Signature):
        """Answer questions about cooking."""
        question = dspy.InputField()
        answer = dspy.OutputField()

    # Use with chef model
    dspy.settings.configure(lm=lms["chef"])
    qa = dspy.Predict(SimpleQA)
    result = qa(question="What are three budget-friendly ingredients?")
    print(f"Chef answer: {result.answer}")
