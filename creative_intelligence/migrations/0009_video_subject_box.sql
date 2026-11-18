-- Where the product sits in a clip ([x1, y1, x2, y2] as fractions of the 9:16 frame, first and last
-- frame combined), so text avoids it and the 4:5 version is framed around it (Video Studio V1).
ALTER TABLE adstudio_videos ADD COLUMN subject_box TEXT;
ALTER TABLE adstudio_video_renders ADD COLUMN note TEXT NOT NULL DEFAULT '';
