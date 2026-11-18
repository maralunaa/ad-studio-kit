-- Knowledge base (K2a): a hook's playbook hook type (its opening move, separate from the Hook University
-- archetype) and the prompt and knowledge versions it was written under.
ALTER TABLE adstudio_hooks ADD COLUMN hook_type TEXT NOT NULL DEFAULT '';
ALTER TABLE adstudio_hooks ADD COLUMN prompt_version TEXT NOT NULL DEFAULT '';
