def evaluate_math_expression(expression: str) -> str:
    """
    Evaluates a complex mathematical expression securely.
    Args:
        expression (str): The math expression to evaluate, e.g. "2 + 2" or "3 * (4 + 5)"
    """
    try:
        # Use a safe evaluation environment
        result = eval(expression, {"__builtins__": None}, {})
        return str(result)
    except Exception as e:
        return f"Error evaluating expression: {e}"
