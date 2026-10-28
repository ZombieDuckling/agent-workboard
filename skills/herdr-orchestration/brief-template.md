# OpenCode brief template (Simplified Technical English)

Copy this, fill every <field>, and send it from a file.

```
READ ALL OF THESE INSTRUCTIONS BEFORE YOU START. DO THE STEPS IN THE GIVEN SEQUENCE. DO NOT DO A STEP THAT IS NOT IN THIS LIST.

1. GENERAL DATA
1.1. You are an agent. You do one task. The task is: <one sentence>
1.2. Your work folder is: <absolute path>
1.3. Do all commands in the work folder. Do not use the command "cd" to go to a different folder.
1.4. <Define each term you use: what "site", "staging", "committed file" mean here.>
1.5. You cannot see pictures. Compare only names, sizes, and numbers.

2. SAFETY RULES
2.1. Do not change <files and folders>.
2.2. Do not use these commands: <list>.
2.3. Do not write a file outside the folder <folder>.
2.4. Do not ask a question. If a step does not give the expected result, do paragraph 5.
2.5. After each step in paragraph 3 is complete, run this command: workboard comment <card-id> -a <worker-name> -n "<step number>: <result>"

3. PROCEDURE
3.1. Run this command: <exact command>
3.2. Wait until the command stops. The command can run for <N> minutes. Do not run it again while it runs.
3.3. Expected result: <exact text or pattern>. Write it down.
3.4. <Next step, with its exact command and expected result.>

4. REPORT
4.1. Write the report to the file: <path>
4.2. The report must contain these sections, in this sequence: <titles and exactly what goes in each>
4.3. Each statement must come from a line in an output file. Do not guess. Do not give a cause. Do not give a recommendation. If you do not know, write "NOT KNOWN".
4.4. Reply with these items only: <items>. Then stop.

5. IF A STEP FAILS
5.1. If <failure sign>, copy the last 15 lines of the output into your reply. Then stop. Do not repair the script.
5.2. If <other failure sign>, <one exact recovery action, once only>.
```

Writing rules: short sentences (20 words at most for an instruction). Active voice. Present tense. One
instruction per sentence. Use the same word for the same thing every time. No idioms, no "should", no
"etc.". Give numbers as digits.
