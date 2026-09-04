# Route quality eval

Routes with human-judged "would you run this" labels. Separate from `golden/`, which
checks that measurements did not change: this checks whether the plan is any good.
Used to tune the ~6 custom-model priority parameters (scope 7.1) and to catch arbitration
regressions that leave every number in range but the route unrunnable.
