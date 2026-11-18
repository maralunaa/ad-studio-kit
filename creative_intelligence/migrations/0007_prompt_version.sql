-- Which prompt files (and versions) built each generated picture: "name@vN+name@vM" (prompts/).
ALTER TABLE adstudio_images ADD COLUMN prompt_version TEXT;
ALTER TABLE adstudio_textads ADD COLUMN prompt_version TEXT;
ALTER TABLE adstudio_core ADD COLUMN prompt_version TEXT;
