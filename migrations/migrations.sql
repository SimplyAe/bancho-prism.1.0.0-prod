# This file contains any sql updates, along with the
# version they are required from. Touching this without
# at least reading utils/updater.py is certainly a bad idea :)

# v3.0.6
alter table users change name_safe safe_name varchar(32) not null;
alter table users drop key users_name_safe_uindex;
alter table users add constraint users_safe_name_uindex unique (safe_name);
alter table users change pw_hash pw_bcrypt char(60) not null;
insert into channels (name, topic, read_priv, write_priv, auto_join) values
  ('#supporter', 'General discussion for p2w gamers.', 48, 48, false),
  ('#staff', 'General discussion for the cool kids.', 28672, 28672, true),
  ('#admin', 'General discussion for the cool.', 24576, 24576, true),
  ('#dev',   'General discussion for the.', 16384, 16384, true);

# v3.0.8
alter table users modify safe_name varchar(32) charset utf8 not null;
alter table users modify name varchar(32) charset utf8 not null;
alter table mail modify msg varchar(2048) charset utf8 not null;
alter table logs modify msg varchar(2048) charset utf8 not null;
drop table if exists comments;
create table comments
(
	id int auto_increment
		primary key,
	target_id int not null comment 'replay, map, or set id',
	target_type enum('replay', 'map', 'song') not null,
	userid int not null,
	time int not null,
	comment varchar(80) charset utf8 not null,
	colour char(6) null comment 'rgb hex string'
);

# v3.0.9
alter table stats modify tscore_vn_std int unsigned default 0 not null;
alter table stats modify tscore_vn_taiko int unsigned default 0 not null;
alter table stats modify tscore_vn_catch int unsigned default 0 not null;
alter table stats modify tscore_vn_mania int unsigned default 0 not null;
alter table stats modify tscore_rx_std int unsigned default 0 not null;
alter table stats modify tscore_rx_taiko int unsigned default 0 not null;
alter table stats modify tscore_rx_catch int unsigned default 0 not null;
alter table stats modify tscore_ap_std int unsigned default 0 not null;
alter table stats modify rscore_vn_std int unsigned default 0 not null;
alter table stats modify rscore_vn_taiko int unsigned default 0 not null;
alter table stats modify rscore_vn_catch int unsigned default 0 not null;
alter table stats modify rscore_vn_mania int unsigned default 0 not null;
alter table stats modify rscore_rx_std int unsigned default 0 not null;
alter table stats modify rscore_rx_taiko int unsigned default 0 not null;
alter table stats modify rscore_rx_catch int unsigned default 0 not null;
alter table stats modify rscore_ap_std int unsigned default 0 not null;
alter table stats modify pp_vn_std smallint unsigned default 0 not null;
alter table stats modify pp_vn_taiko smallint unsigned default 0 not null;
alter table stats modify pp_vn_catch smallint unsigned default 0 not null;
alter table stats modify pp_vn_mania smallint unsigned default 0 not null;
alter table stats modify pp_rx_std smallint unsigned default 0 not null;
alter table stats modify pp_rx_taiko smallint unsigned default 0 not null;
alter table stats modify pp_rx_catch smallint unsigned default 0 not null;
alter table stats modify pp_ap_std smallint unsigned default 0 not null;
alter table stats modify plays_vn_std int unsigned default 0 not null;
alter table stats modify plays_vn_taiko int unsigned default 0 not null;
alter table stats modify plays_vn_catch int unsigned default 0 not null;
alter table stats modify plays_vn_mania int unsigned default 0 not null;
alter table stats modify plays_rx_std int unsigned default 0 not null;
alter table stats modify plays_rx_taiko int unsigned default 0 not null;
alter table stats modify plays_rx_catch int unsigned default 0 not null;
alter table stats modify plays_ap_std int unsigned default 0 not null;
alter table stats modify playtime_vn_std int unsigned default 0 not null;
alter table stats modify playtime_vn_taiko int unsigned default 0 not null;
alter table stats modify playtime_vn_catch int unsigned default 0 not null;
alter table stats modify playtime_vn_mania int unsigned default 0 not null;
alter table stats modify playtime_rx_std int unsigned default 0 not null;
alter table stats modify playtime_rx_taiko int unsigned default 0 not null;
alter table stats modify playtime_rx_catch int unsigned default 0 not null;
alter table stats modify playtime_ap_std int unsigned default 0 not null;
alter table stats modify maxcombo_vn_std int unsigned default 0 not null;
alter table stats modify maxcombo_vn_taiko int unsigned default 0 not null;
alter table stats modify maxcombo_vn_catch int unsigned default 0 not null;
alter table stats modify maxcombo_vn_mania int unsigned default 0 not null;
alter table stats modify maxcombo_rx_std int unsigned default 0 not null;
alter table stats modify maxcombo_rx_taiko int unsigned default 0 not null;
alter table stats modify maxcombo_rx_catch int unsigned default 0 not null;
alter table stats modify maxcombo_ap_std int unsigned default 0 not null;

# v3.0.10
update channels set write_priv = 24576 where name = '#announce';

# v3.1.0
alter table maps modify bpm float(12,2) default 0.00 not null;
alter table stats modify tscore_vn_std bigint unsigned default 0 not null;
alter table stats modify tscore_vn_taiko bigint unsigned default 0 not null;
alter table stats modify tscore_vn_catch bigint unsigned default 0 not null;
alter table stats modify tscore_vn_mania bigint unsigned default 0 not null;
alter table stats modify tscore_rx_std bigint unsigned default 0 not null;
alter table stats modify tscore_rx_taiko bigint unsigned default 0 not null;
alter table stats modify tscore_rx_catch bigint unsigned default 0 not null;
alter table stats modify tscore_ap_std bigint unsigned default 0 not null;
alter table stats modify rscore_vn_std bigint unsigned default 0 not null;
alter table stats modify rscore_vn_taiko bigint unsigned default 0 not null;
alter table stats modify rscore_vn_catch bigint unsigned default 0 not null;
alter table stats modify rscore_vn_mania bigint unsigned default 0 not null;
alter table stats modify rscore_rx_std bigint unsigned default 0 not null;
alter table stats modify rscore_rx_taiko bigint unsigned default 0 not null;
alter table stats modify rscore_rx_catch bigint unsigned default 0 not null;
alter table stats modify rscore_ap_std bigint unsigned default 0 not null;
alter table stats modify pp_vn_std int unsigned default 0 not null;
alter table stats modify pp_vn_taiko int unsigned default 0 not null;
alter table stats modify pp_vn_catch int unsigned default 0 not null;
alter table stats modify pp_vn_mania int unsigned default 0 not null;
alter table stats modify pp_rx_std int unsigned default 0 not null;
alter table stats modify pp_rx_taiko int unsigned default 0 not null;
alter table stats modify pp_rx_catch int unsigned default 0 not null;
alter table stats modify pp_ap_std int unsigned default 0 not null;

# v3.1.2
create table clans
(
	id int auto_increment
		primary key,
	name varchar(16) not null,
	tag varchar(6) not null,
	owner int not null,
	created_at datetime not null,
	constraint clans_name_uindex
		unique (name),
	constraint clans_owner_uindex
		unique (owner),
	constraint clans_tag_uindex
		unique (tag)
);
alter table users add clan_id int default 0 not null;
alter table users add clan_rank tinyint(1) default 0 not null;
create table achievements
(
	id int auto_increment
		primary key,
	file varchar(128) not null,
	name varchar(128) not null,
	`desc` varchar(256) not null,
	cond varchar(64) not null,
	mode tinyint(1) not null,
	constraint achievements_desc_uindex
		unique (`desc`),
	constraint achievements_file_uindex
		unique (file),
	constraint achievements_name_uindex
		unique (name)
);
create table user_achievements
(
	userid int not null,
	achid int not null,
	primary key (userid, achid)
);
insert into achievements (`id`, `file`, `name`, `desc`, `cond`, `mode`) values (1, 'osu-skill-pass-1', 'Rising Star', 'Can''t go forward without the first steps.', '(score.mods & 259 == 0) and 2 >= score.sr > 1', 0);
insert into achievements (`id`, `file`, `name`, `desc`, `cond`, `mode`) values (2, 'osu-skill-pass-2', 'Constellation Prize', 'Definitely not a consolation prize. Now things start getting hard!', '(score.mods & 259 == 0) and 3 >= score.sr > 2', 0);
insert into achievements (`id`, `file`, `name`, `desc`, `cond`, `mode`) values (3, 'osu-skill-pass-3', 'Building Confidence', 'Oh, you''ve SO got this.', '(score.mods & 259 == 0) and 4 >= score.sr > 3', 0);
insert into achievements (`id`, `file`, `name`, `desc`, `cond`, `mode`) values (4, 'osu-skill-pass-4', 'Insanity Approaches', 'You''re not twitching, you''re just ready.', '(score.mods & 259 == 0) and 5 >= score.sr > 4', 0);
insert into achievements (`id`, `file`, `name`, `desc`, `cond`, `mode`) values (5, 'osu-skill-pass-5', 'These Clarion Skies', 'Everything seems so clear now.', '(score.mods & 259 == 0) and 6 >= score.sr > 5', 0);
insert into achievements (`id`, `file`, `name`, `desc`, `cond`, `mode`) values (6, 'osu-skill-pass-6', 'Above and Beyond', 'A cut above the rest.', '(score.mods & 259 == 0) and 7 >= score.sr > 6', 0);
insert into achievements (`id`, `file`, `name`, `desc`, `cond`, `mode`) values (7, 'osu-skill-pass-7', 'Supremacy', 'All marvel before your prowess.', '(score.mods & 259 == 0) and 8 >= score.sr > 7', 0);
insert into achievements (`id`, `file`, `name`, `desc`, `cond`, `mode`) values (8, 'osu-skill-pass-8', 'Absolution', 'My god, you''re full of stars!', '(score.mods & 259 == 0) and 9 >= score.sr > 8', 0);
insert into achievements (`id`, `file`, `name`, `desc`, `cond`, `mode`) values (9, 'osu-skill-pass-9', 'Event Horizon', 'No force dares to pull you under.', '(score.mods & 259 == 0) and 10 >= score.sr > 9', 0);
insert into achievements (`id`, `file`, `name`, `desc`, `cond`, `mode`) values (10, 'osu-skill-pass-10', 'Phantasm', 'Fevered is your passion, extraordinary is your skill.', '(score.mods & 259 == 0) and 11 >= score.sr > 10', 0);
insert into achievements (`id`, `file`, `name`, `desc`, `cond`, `mode`) values (11, 'osu-skill-fc-1', 'Totality', 'All the notes. Every single one.', 'score.perfect and 2 >= score.sr > 1', 0);
insert into achievements (`id`, `file`, `name`, `desc`, `cond`, `mode`) values (12, 'osu-skill-fc-2', 'Business As Usual', 'Two to go, please.', 'score.perfect and 3 >= score.sr > 2', 0);
insert into achievements (`id`, `file`, `name`, `desc`, `cond`, `mode`) values (13, 'osu-skill-fc-3', 'Building Steam', 'Hey, this isn''t so bad.', 'score.perfect and 4 >= score.sr > 3', 0);
insert into achievements (`id`, `file`, `name`, `desc`, `cond`, `mode`) values (14, 'osu-skill-fc-4', 'Moving Forward', 'Bet you feel good about that.', 'score.perfect and 5 >= score.sr > 4', 0);
insert into achievements (`id`, `file`, `name`, `desc`, `cond`, `mode`) values (15, 'osu-skill-fc-5', 'Paradigm Shift', 'Surprisingly difficult.', 'score.perfect and 6 >= score.sr > 5', 0);
insert into achievements (`id`, `file`, `name`, `desc`, `cond`, `mode`) values (16, 'osu-skill-fc-6', 'Anguish Quelled', 'Don''t choke.', 'score.perfect and 7 >= score.sr > 6', 0);
insert into achievements (`id`, `file`, `name`, `desc`, `cond`, `mode`) values (17, 'osu-skill-fc-7', 'Never Give Up', 'Excellence is its own reward.', 'score.perfect and 8 >= score.sr > 7', 0);
insert into achievements (`id`, `file`, `name`, `desc`, `cond`, `mode`) values (18, 'osu-skill-fc-8', 'Aberration', 'They said it couldn''t be done. They were wrong.', 'score.perfect and 9 >= score.sr > 8', 0);
insert into achievements (`id`, `file`, `name`, `desc`, `cond`, `mode`) values (19, 'osu-skill-fc-9', 'Chosen', 'Reign among the Prometheans, where you belong.', 'score.perfect and 10 >= score.sr > 9', 0);
insert into achievements (`id`, `file`, `name`, `desc`, `cond`, `mode`) values (20, 'osu-skill-fc-10', 'Unfathomable', 'You have no equal.', 'score.perfect and 11 >= score.sr > 10', 0);
insert into achievements (`id`, `file`, `name`, `desc`, `cond`, `mode`) values (21, 'osu-combo-500', '500 Combo', '500 big ones! You''re moving up in the world!', '750 >= score.max_combo > 500', 0);
insert into achievements (`id`, `file`, `name`, `desc`, `cond`, `mode`) values (22, 'osu-combo-750', '750 Combo', '750 notes back to back? Woah.', '1000 >= score.max_combo > 750', 0);
insert into achievements (`id`, `file`, `name`, `desc`, `cond`, `mode`) values (23, 'osu-combo-1000', '1000 Combo', 'A thousand reasons why you rock at this game.', '2000 >= score.max_combo > 1000', 0);
insert into achievements (`id`, `file`, `name`, `desc`, `cond`, `mode`) values (24, 'osu-combo-2000', '2000 Combo', 'Nothing can stop you now.', 'score.max_combo >= 2000', 0);
insert into achievements (`id`, `file`, `name`, `desc`, `cond`, `mode`) values (25, 'taiko-skill-pass-1', 'My First Don', 'Marching to the beat of your own drum. Literally.', '(score.mods & 259 == 0) and 2 >= score.sr > 1', 1);
insert into achievements (`id`, `file`, `name`, `desc`, `cond`, `mode`) values (26, 'taiko-skill-pass-2', 'Katsu Katsu Katsu', 'Hora! Izuko!', '(score.mods & 259 == 0) and 3 >= score.sr > 2', 1);
insert into achievements (`id`, `file`, `name`, `desc`, `cond`, `mode`) values (27, 'taiko-skill-pass-3', 'Not Even Trying', 'Muzukashii? Not even.', '(score.mods & 259 == 0) and 4 >= score.sr > 3', 1);
insert into achievements (`id`, `file`, `name`, `desc`, `cond`, `mode`) values (28, 'taiko-skill-pass-4', 'Face Your Demons', 'The first trials are now behind you, but are you a match for the Oni?', '(score.mods & 259 == 0) and 5 >= score.sr > 4', 1);
insert into achievements (`id`, `file`, `name`, `desc`, `cond`, `mode`) values (29, 'taiko-skill-pass-5', 'The Demon Within', 'No rest for the wicked.', '(score.mods & 259 == 0) and 6 >= score.sr > 5', 1);
insert into achievements (`id`, `file`, `name`, `desc`, `cond`, `mode`) values (30, 'taiko-skill-pass-6', 'Drumbreaker', 'Too strong.', '(score.mods & 259 == 0) and 7 >= score.sr > 6', 1);
insert into achievements (`id`, `file`, `name`, `desc`, `cond`, `mode`) values (31, 'taiko-skill-pass-7', 'The Godfather', 'You are the Don of Dons.', '(score.mods & 259 == 0) and 8 >= score.sr > 7', 1);
insert into achievements (`id`, `file`, `name`, `desc`, `cond`, `mode`) values (32, 'taiko-skill-pass-8', 'Rhythm Incarnate', 'Feel the beat. Become the beat.', '(score.mods & 259 == 0) and 9 >= score.sr > 8', 1);
insert into achievements (`id`, `file`, `name`, `desc`, `cond`, `mode`) values (33, 'taiko-skill-fc-1', 'Keeping Time', 'Don, then katsu. Don, then katsu..', 'score.perfect and 2 >= score.sr > 1', 1);
insert into achievements (`id`, `file`, `name`, `desc`, `cond`, `mode`) values (34, 'taiko-skill-fc-2', 'To Your Own Beat', 'Straight and steady.', 'score.perfect and 3 >= score.sr > 2', 1);
insert into achievements (`id`, `file`, `name`, `desc`, `cond`, `mode`) values (35, 'taiko-skill-fc-3', 'Big Drums', 'Bigger scores to match.', 'score.perfect and 4 >= score.sr > 3', 1);
insert into achievements (`id`, `file`, `name`, `desc`, `cond`, `mode`) values (36, 'taiko-skill-fc-4', 'Adversity Overcome', 'Difficult? Not for you.', 'score.perfect and 5 >= score.sr > 4', 1);
insert into achievements (`id`, `file`, `name`, `desc`, `cond`, `mode`) values (37, 'taiko-skill-fc-5', 'Demonslayer', 'An Oni felled forevermore.', 'score.perfect and 6 >= score.sr > 5', 1);
insert into achievements (`id`, `file`, `name`, `desc`, `cond`, `mode`) values (38, 'taiko-skill-fc-6', 'Rhythm''s Call', 'Heralding true skill.', 'score.perfect and 7 >= score.sr > 6', 1);
insert into achievements (`id`, `file`, `name`, `desc`, `cond`, `mode`) values (39, 'taiko-skill-fc-7', 'Time Everlasting', 'Not a single beat escapes you.', 'score.perfect and 8 >= score.sr > 7', 1);
insert into achievements (`id`, `file`, `name`, `desc`, `cond`, `mode`) values (40, 'taiko-skill-fc-8', 'The Drummer''s Throne', 'Percussive brilliance befitting royalty alone.', 'score.perfect and 9 >= score.sr > 8', 1);
insert into achievements (`id`, `file`, `name`, `desc`, `cond`, `mode`) values (41, 'fruits-skill-pass-1', 'A Slice Of Life', 'Hey, this fruit catching business isn''t bad.', '(score.mods & 259 == 0) and 2 >= score.sr > 1', 2);
insert into achievements (`id`, `file`, `name`, `desc`, `cond`, `mode`) values (42, 'fruits-skill-pass-2', 'Dashing Ever Forward', 'Fast is how you do it.', '(score.mods & 259 == 0) and 3 >= score.sr > 2', 2);
insert into achievements (`id`, `file`, `name`, `desc`, `cond`, `mode`) values (43, 'fruits-skill-pass-3', 'Zesty Disposition', 'No scurvy for you, not with that much fruit.', '(score.mods & 259 == 0) and 4 >= score.sr > 3', 2);
insert into achievements (`id`, `file`, `name`, `desc`, `cond`, `mode`) values (44, 'fruits-skill-pass-4', 'Hyperdash ON!', 'Time and distance is no obstacle to you.', '(score.mods & 259 == 0) and 5 >= score.sr > 4', 2);
insert into achievements (`id`, `file`, `name`, `desc`, `cond`, `mode`) values (45, 'fruits-skill-pass-5', 'It''s Raining Fruit', 'And you can catch them all.', '(score.mods & 259 == 0) and 6 >= score.sr > 5', 2);
insert into achievements (`id`, `file`, `name`, `desc`, `cond`, `mode`) values (46, 'fruits-skill-pass-6', 'Fruit Ninja', 'Legendary techniques.', '(score.mods & 259 == 0) and 7 >= score.sr > 6', 2);
insert into achievements (`id`, `file`, `name`, `desc`, `cond`, `mode`) values (47, 'fruits-skill-pass-7', 'Dreamcatcher', 'No fruit, only dreams now.', '(score.mods & 259 == 0) and 8 >= score.sr > 7', 2);
insert into achievements (`id`, `file`, `name`, `desc`, `cond`, `mode`) values (48, 'fruits-skill-pass-8', 'Lord of the Catch', 'Your kingdom kneels before you.', '(score.mods & 259 == 0) and 9 >= score.sr > 8', 2);
insert into achievements (`id`, `file`, `name`, `desc`, `cond`, `mode`) values (49, 'fruits-skill-fc-1', 'Sweet And Sour', 'Apples and oranges, literally.', 'score.perfect and 2 >= score.sr > 1', 2);
insert into achievements (`id`, `file`, `name`, `desc`, `cond`, `mode`) values (50, 'fruits-skill-fc-2', 'Reaching The Core', 'The seeds of future success.', 'score.perfect and 3 >= score.sr > 2', 2);
insert into achievements (`id`, `file`, `name`, `desc`, `cond`, `mode`) values (51, 'fruits-skill-fc-3', 'Clean Platter', 'Clean only of failure. It is completely full, otherwise.', 'score.perfect and 4 >= score.sr > 3', 2);
insert into achievements (`id`, `file`, `name`, `desc`, `cond`, `mode`) values (52, 'fruits-skill-fc-4', 'Between The Rain', 'No umbrella needed.', 'score.perfect and 5 >= score.sr > 4', 2);
insert into achievements (`id`, `file`, `name`, `desc`, `cond`, `mode`) values (53, 'fruits-skill-fc-5', 'Addicted', 'That was an overdose?', 'score.perfect and 6 >= score.sr > 5', 2);
insert into achievements (`id`, `file`, `name`, `desc`, `cond`, `mode`) values (54, 'fruits-skill-fc-6', 'Quickening', 'A dash above normal limits.', 'score.perfect and 7 >= score.sr > 6', 2);
insert into achievements (`id`, `file`, `name`, `desc`, `cond`, `mode`) values (55, 'fruits-skill-fc-7', 'Supersonic', 'Faster than is reasonably necessary.', 'score.perfect and 8 >= score.sr > 7', 2);
insert into achievements (`id`, `file`, `name`, `desc`, `cond`, `mode`) values (56, 'fruits-skill-fc-8', 'Dashing Scarlet', 'Speed beyond mortal reckoning.', 'score.perfect and 9 >= score.sr > 8', 2);
insert into achievements (`id`, `file`, `name`, `desc`, `cond`, `mode`) values (57, 'mania-skill-pass-1', 'First Steps', 'It isn''t 9-to-5, but 1-to-9. Keys, that is.', '(score.mods & 259 == 0) and 2 >= score.sr > 1', 3);
insert into achievements (`id`, `file`, `name`, `desc`, `cond`, `mode`) values (58, 'mania-skill-pass-2', 'No Normal Player', 'Not anymore, at least.', '(score.mods & 259 == 0) and 3 >= score.sr > 2', 3);
insert into achievements (`id`, `file`, `name`, `desc`, `cond`, `mode`) values (59, 'mania-skill-pass-3', 'Impulse Drive', 'Not quite hyperspeed, but getting close.', '(score.mods & 259 == 0) and 4 >= score.sr > 3', 3);
insert into achievements (`id`, `file`, `name`, `desc`, `cond`, `mode`) values (60, 'mania-skill-pass-4', 'Hyperspeed', 'Woah.', '(score.mods & 259 == 0) and 5 >= score.sr > 4', 3);
insert into achievements (`id`, `file`, `name`, `desc`, `cond`, `mode`) values (61, 'mania-skill-pass-5', 'Ever Onwards', 'Another challenge is just around the corner.', '(score.mods & 259 == 0) and 6 >= score.sr > 5', 3);
insert into achievements (`id`, `file`, `name`, `desc`, `cond`, `mode`) values (62, 'mania-skill-pass-6', 'Another Surpassed', 'Is there no limit to your skills?', '(score.mods & 259 == 0) and 7 >= score.sr > 6', 3);
insert into achievements (`id`, `file`, `name`, `desc`, `cond`, `mode`) values (63, 'mania-skill-pass-7', 'Extra Credit', 'See me after class.', '(score.mods & 259 == 0) and 8 >= score.sr > 7', 3);
insert into achievements (`id`, `file`, `name`, `desc`, `cond`, `mode`) values (64, 'mania-skill-pass-8', 'Maniac', 'There''s just no stopping you.', '(score.mods & 259 == 0) and 9 >= score.sr > 8', 3);
insert into achievements (`id`, `file`, `name`, `desc`, `cond`, `mode`) values (65, 'mania-skill-fc-1', 'Keystruck', 'The beginning of a new story', 'score.perfect and (score.mods & 259 == 0) and 2 >= score.sr > 1', 3);
insert into achievements (`id`, `file`, `name`, `desc`, `cond`, `mode`) values (66, 'mania-skill-fc-2', 'Keying In', 'Finding your groove.', 'score.perfect and 3 >= score.sr > 2', 3);
insert into achievements (`id`, `file`, `name`, `desc`, `cond`, `mode`) values (67, 'mania-skill-fc-3', 'Hyperflow', 'You can *feel* the rhythm.', 'score.perfect and 4 >= score.sr > 3', 3);
insert into achievements (`id`, `file`, `name`, `desc`, `cond`, `mode`) values (68, 'mania-skill-fc-4', 'Breakthrough', 'Many skills mastered, rolled into one.', 'score.perfect and 5 >= score.sr > 4', 3);
insert into achievements (`id`, `file`, `name`, `desc`, `cond`, `mode`) values (69, 'mania-skill-fc-5', 'Everything Extra', 'Giving your all is giving everything you have.', 'score.perfect and 6 >= score.sr > 5', 3);
insert into achievements (`id`, `file`, `name`, `desc`, `cond`, `mode`) values (70, 'mania-skill-fc-6', 'Level Breaker', 'Finesse beyond reason', 'score.perfect and 7 >= score.sr > 6', 3);
insert into achievements (`id`, `file`, `name`, `desc`, `cond`, `mode`) values (71, 'mania-skill-fc-7', 'Step Up', 'A precipice rarely seen.', 'score.perfect and 8 >= score.sr > 7', 3);
insert into achievements (`id`, `file`, `name`, `desc`, `cond`, `mode`) values (72, 'mania-skill-fc-8', 'Behind The Veil', 'Supernatural!', 'score.perfect and 9 >= score.sr > 8', 3);

# v3.1.3
alter table clans modify name varchar(16) charset utf8 not null;
alter table clans modify tag varchar(6) charset utf8 not null;
alter table achievements modify name varchar(128) charset utf8 not null;
alter table achievements modify `desc` varchar(256) charset utf8 not null;
alter table maps modify artist varchar(128) charset utf8 not null;
alter table maps modify title varchar(128) charset utf8 not null;
alter table maps modify version varchar(128) charset utf8 not null;
alter table maps modify creator varchar(19) charset utf8 not null comment 'not 100%% certain on len';
alter table tourney_pools drop foreign key tourney_pools_users_id_fk;
alter table tourney_pool_maps drop foreign key tourney_pool_maps_tourney_pools_id_fk;
alter table stats drop foreign key stats_users_id_fk;
alter table ratings drop foreign key ratings_maps_md5_fk;
alter table ratings drop foreign key ratings_users_id_fk;
alter table logs modify `from` int not null comment 'both from and to are playerids';

# v3.1.9
alter table scores_rx modify id bigint(20) unsigned auto_increment;
update scores_rx set id = id + (6148914691236517205 - 1);
select @max_rx := MAX(id) + 1 from scores_rx;
set @s = CONCAT('alter table scores_rx auto_increment = ', @max_rx);
prepare stmt from @s;
execute stmt;
deallocate PREPARE stmt;
alter table scores_ap modify id bigint(20) unsigned auto_increment;
update scores_ap set id = id + (12297829382473034410 - 1);
select @max_ap := MAX(id) + 1 from scores_ap;
set @s = CONCAT('alter table scores_ap auto_increment = ', @max_ap);
prepare stmt from @s;
execute stmt;
deallocate PREPARE stmt;
alter table performance_reports modify scoreid bigint(20) unsigned auto_increment;

# v3.2.0
create table map_requests
(
	id int auto_increment
		primary key,
	map_id int not null,
	player_id int not null,
	datetime datetime not null,
	active tinyint(1) not null
);

# v3.2.1
update scores_rx set id = id - 3074457345618258603;
update scores_ap set id = id - 6148914691236517206;

# v3.2.2
alter table maps add max_combo int not null after total_length;
alter table users change clan_rank clan_priv tinyint(1) default 0 not null;

# v3.2.3
alter table users add api_key char(36) default NULL null;
create unique index users_api_key_uindex on users (api_key);

# v3.2.4
update achievements set file = replace(file, 'ctb', 'fruits') where mode = 2;

# v3.2.5
update achievements set cond = '(score.mods & 1 == 0) and 1 <= score.sr < 2' where file in ('osu-skill-pass-1', 'taiko-skill-pass-1', 'fruits-skill-pass-1', 'mania-skill-pass-1');
update achievements set cond = '(score.mods & 1 == 0) and 2 <= score.sr < 3' where file in ('osu-skill-pass-2', 'taiko-skill-pass-2', 'fruits-skill-pass-2', 'mania-skill-pass-2');
update achievements set cond = '(score.mods & 1 == 0) and 3 <= score.sr < 4' where file in ('osu-skill-pass-3', 'taiko-skill-pass-3', 'fruits-skill-pass-3', 'mania-skill-pass-3');
update achievements set cond = '(score.mods & 1 == 0) and 4 <= score.sr < 5' where file in ('osu-skill-pass-4', 'taiko-skill-pass-4', 'fruits-skill-pass-4', 'mania-skill-pass-4');
update achievements set cond = '(score.mods & 1 == 0) and 5 <= score.sr < 6' where file in ('osu-skill-pass-5', 'taiko-skill-pass-5', 'fruits-skill-pass-5', 'mania-skill-pass-5');
update achievements set cond = '(score.mods & 1 == 0) and 6 <= score.sr < 7' where file in ('osu-skill-pass-6', 'taiko-skill-pass-6', 'fruits-skill-pass-6', 'mania-skill-pass-6');
update achievements set cond = '(score.mods & 1 == 0) and 7 <= score.sr < 8' where file in ('osu-skill-pass-7', 'taiko-skill-pass-7', 'fruits-skill-pass-7', 'mania-skill-pass-7');
update achievements set cond = '(score.mods & 1 == 0) and 8 <= score.sr < 9' where file in ('osu-skill-pass-8', 'taiko-skill-pass-8', 'fruits-skill-pass-8', 'mania-skill-pass-8');
update achievements set cond = '(score.mods & 1 == 0) and 9 <= score.sr < 10' where file = 'osu-skill-pass-9';
update achievements set cond = '(score.mods & 1 == 0) and 10 <= score.sr < 11' where file = 'osu-skill-pass-10';

update achievements set cond = 'score.perfect and 1 <= score.sr < 2' where file in ('osu-skill-fc-1', 'taiko-skill-fc-1', 'fruits-skill-fc-1', 'mania-skill-fc-1');
update achievements set cond = 'score.perfect and 2 <= score.sr < 3' where file in ('osu-skill-fc-2', 'taiko-skill-fc-2', 'fruits-skill-fc-2', 'mania-skill-fc-2');
update achievements set cond = 'score.perfect and 3 <= score.sr < 4' where file in ('osu-skill-fc-3', 'taiko-skill-fc-3', 'fruits-skill-fc-3', 'mania-skill-fc-3');
update achievements set cond = 'score.perfect and 4 <= score.sr < 5' where file in ('osu-skill-fc-4', 'taiko-skill-fc-4', 'fruits-skill-fc-4', 'mania-skill-fc-4');
update achievements set cond = 'score.perfect and 5 <= score.sr < 6' where file in ('osu-skill-fc-5', 'taiko-skill-fc-5', 'fruits-skill-fc-5', 'mania-skill-fc-5');
update achievements set cond = 'score.perfect and 6 <= score.sr < 7' where file in ('osu-skill-fc-6', 'taiko-skill-fc-6', 'fruits-skill-fc-6', 'mania-skill-fc-6');
update achievements set cond = 'score.perfect and 7 <= score.sr < 8' where file in ('osu-skill-fc-7', 'taiko-skill-fc-7', 'fruits-skill-fc-7', 'mania-skill-fc-7');
update achievements set cond = 'score.perfect and 8 <= score.sr < 9' where file in ('osu-skill-fc-8', 'taiko-skill-fc-8', 'fruits-skill-fc-8', 'mania-skill-fc-8');
update achievements set cond = 'score.perfect and 9 <= score.sr < 10' where file = 'osu-skill-fc-9';
update achievements set cond = 'score.perfect and 10 <= score.sr < 11' where file = 'osu-skill-fc-10';

update achievements set cond = '500 <= score.max_combo < 750' where file = 'osu-combo-500';
update achievements set cond = '750 <= score.max_combo < 1000' where file = 'osu-combo-750';
update achievements set cond = '1000 <= score.max_combo < 2000' where file = 'osu-combo-1000';
update achievements set cond = '2000 <= score.max_combo' where file = 'osu-combo-2000';

# v3.2.6
alter table stats change maxcombo_vn_std max_combo_vn_std int unsigned default 0 not null;
alter table stats change maxcombo_vn_taiko max_combo_vn_taiko int unsigned default 0 not null;
alter table stats change maxcombo_vn_catch max_combo_vn_catch int unsigned default 0 not null;
alter table stats change maxcombo_vn_mania max_combo_vn_mania int unsigned default 0 not null;
alter table stats change maxcombo_rx_std max_combo_rx_std int unsigned default 0 not null;
alter table stats change maxcombo_rx_taiko max_combo_rx_taiko int unsigned default 0 not null;
alter table stats change maxcombo_rx_catch max_combo_rx_catch int unsigned default 0 not null;
alter table stats change maxcombo_ap_std max_combo_ap_std int unsigned default 0 not null;

# v3.2.7
drop table if exists user_hashes;

# v3.3.0
rename table friendships to relationships;
alter table relationships add type enum('friend', 'block') not null;

# v3.3.1
create table ingame_logins
(
	id int auto_increment
		primary key,
	userid int not null,
	ip varchar(45) not null comment 'maxlen for ipv6',
	osu_ver date not null,
	osu_stream varchar(11) not null,
	datetime datetime not null
);

# v3.3.7
update achievements set cond = CONCAT(cond, ' and mode_vn == 0') where mode = 0;
update achievements set cond = CONCAT(cond, ' and mode_vn == 1') where mode = 1;
update achievements set cond = CONCAT(cond, ' and mode_vn == 2') where mode = 2;
update achievements set cond = CONCAT(cond, ' and mode_vn == 3') where mode = 3;
alter table achievements drop column mode;

# v3.3.8
create table mapsets
(
	server enum('osu!', 'gulag') default 'osu!' not null,
	id int not null,
	last_osuapi_check datetime default CURRENT_TIMESTAMP not null,
	primary key (server, id),
	constraint nmapsets_id_uindex
		unique (id)
);

# v3.4.1
alter table maps add filename varchar(256) charset utf8 not null after creator;

# v3.5.2
alter table scores_vn add online_checksum char(32) not null;
alter table scores_rx add online_checksum char(32) not null;
alter table scores_ap add online_checksum char(32) not null;

# v4.1.1
alter table stats add total_hits int unsigned default 0 not null after max_combo;

# v4.1.2
alter table stats add replay_views int unsigned default 0 not null after total_hits;

# v4.1.3
alter table users add preferred_mode int default 0 not null after latest_activity;
alter table users add play_style int default 0 not null after preferred_mode;
alter table users add custom_badge_name varchar(16) charset utf8 null after play_style;
alter table users add custom_badge_icon varchar(64) null after custom_badge_name;
alter table users add userpage_content varchar(2048) charset utf8 null after custom_badge_icon;

# v4.2.0
# please refer to tools/migrate_v420 for further v4.2.0 migrations
update stats set mode = 8 where mode = 7;

# v4.3.1
alter table maps change server server enum('osu!', 'private') default 'osu!' not null;
alter table mapsets change server server enum('osu!', 'private') default 'osu!' not null;

# v4.4.2
insert into achievements (id, file, name, `desc`, cond) values (73, 'all-intro-suddendeath', 'Finality', 'High stakes, no regrets.', 'score.mods == 32');
insert into achievements (id, file, name, `desc`, cond) values (74, 'all-intro-hidden', 'Blindsight', 'I can see just perfectly', 'score.mods & 8');
insert into achievements (id, file, name, `desc`, cond) values (75, 'all-intro-perfect', 'Perfectionist', 'Accept nothing but the best.', 'score.mods & 16384');
insert into achievements (id, file, name, `desc`, cond) values (76, 'all-intro-hardrock', 'Rock Around The Clock', "You can\'t stop the rock.", 'score.mods & 16');
insert into achievements (id, file, name, `desc`, cond) values (77, 'all-intro-doubletime', 'Time And A Half', "Having a right ol\' time. One and a half of them, almost.", 'score.mods & 64');
insert into achievements (id, file, name, `desc`, cond) values (78, 'all-intro-flashlight', 'Are You Afraid Of The Dark?', "Harder than it looks, probably because it\'s hard to look.", 'score.mods & 1024');
insert into achievements (id, file, name, `desc`, cond) values (79, 'all-intro-easy', 'Dial It Right Back', 'Sometimes you just want to take it easy.', 'score.mods & 2');
insert into achievements (id, file, name, `desc`, cond) values (80, 'all-intro-nofail', 'Risk Averse', 'Safety nets are fun!', 'score.mods & 1');
insert into achievements (id, file, name, `desc`, cond) values (81, 'all-intro-nightcore', 'Sweet Rave Party', 'Founded in the fine tradition of changing things that were just fine as they were.', 'score.mods & 512');
insert into achievements (id, file, name, `desc`, cond) values (82, 'all-intro-halftime', 'Slowboat', 'You got there. Eventually.', 'score.mods & 256');
insert into achievements (id, file, name, `desc`, cond) values (83, 'all-intro-spunout', 'Burned Out', 'One cannot always spin to win.', 'score.mods & 4096');

# v4.4.3
alter table favourites add created_at int default 0 not null;

# v4.7.1
lock tables maps write;
alter table maps drop primary key;
alter table maps add primary key (id);
alter table maps modify column server enum('osu!', 'private') not null default 'osu!' after id;
unlock tables;

# v5.0.1
create index channels_auto_join_index
	on channels (auto_join);

create index maps_set_id_index
	on maps (set_id);
create index maps_status_index
	on maps (status);
create index maps_filename_index
	on maps (filename);
create index maps_plays_index
	on maps (plays);
create index maps_mode_index
	on maps (mode);
create index maps_frozen_index
	on maps (frozen);

create index scores_map_md5_index
	on scores (map_md5);
create index scores_score_index
	on scores (score);
create index scores_pp_index
	on scores (pp);
create index scores_mods_index
	on scores (mods);
create index scores_status_index
	on scores (status);
create index scores_mode_index
	on scores (mode);
create index scores_play_time_index
	on scores (play_time);
create index scores_userid_index
	on scores (userid);
create index scores_online_checksum_index
	on scores (online_checksum);

create index stats_mode_index
	on stats (mode);
create index stats_pp_index
	on stats (pp);
create index stats_tscore_index
	on stats (tscore);
create index stats_rscore_index
	on stats (rscore);

create index tourney_pool_maps_mods_slot_index
	on tourney_pool_maps (mods, slot);

create index user_achievements_achid_index
	on user_achievements (achid);
create index user_achievements_userid_index
	on user_achievements (userid);

create index users_priv_index
	on users (priv);
create index users_clan_id_index
	on users (clan_id);
create index users_clan_priv_index
	on users (clan_priv);
create index users_country_index
	on users (country);

# v5.2.2
create index scores_fetch_leaderboard_generic_index
	on scores (map_md5, status, mode);

# v5.3.1
# Prism anticheat: per-score replay analysis records (Track 2.3).
# One row per score; the durable "has this been analysed" source of truth that
# the Redis analysis queue sits on top of. `status = replay_missing` is the skip
# path for scores whose .osr never landed, so the queue stops retrying them.
create table score_replay_stats
(
	score_id int not null,
	mode tinyint(1) not null,
	status enum('pending', 'analyzed', 'replay_missing', 'error') default 'pending' not null,
	extractor_version int default 0 not null,
	error_detail varchar(255) null,
	frame_count int default 0 not null,
	duration_ms int default 0 not null,
	tap_count int default 0 not null,
	uses_keyboard tinyint(1) default 0 not null,
	tortuosity float(12,6) default 0.000000 not null,
	jitter_spike_count int default 0 not null,
	robotic_tap_run_count int default 0 not null,
	max_robotic_run_taps int default 0 not null,
	frozen_span_count int default 0 not null,
	straight_run_count int default 0 not null,
	constant_velocity_run_count int default 0 not null,
	features mediumtext null,
	analyzed_at datetime null,
	constraint score_replay_stats_score_id_uindex primary key (score_id)
);
create index score_replay_stats_status_index
	on score_replay_stats (status);
create index score_replay_stats_mode_index
	on score_replay_stats (mode);
create index score_replay_stats_extractor_version_index
	on score_replay_stats (extractor_version);

# v5.3.2
# Prism data foundation: daily per-player, per-mode stat snapshots (Track 3.1).
# One immutable row per (user, mode, day). The live `stats` table only holds the
# current value; rank history, peak rank, and the anticheat behavioural
# baselines all need the past, which is unrecoverable after the fact. Ranks are
# captured from `stats` via a window function over the same ranked predicate the
# leaderboard uses, so a snapshot is durable even when Redis is cold.
create table stat_snapshots
(
	id bigint auto_increment
		primary key,
	user_id int not null,
	mode tinyint(1) not null,
	snapshot_date date not null,
	pp int unsigned default 0 not null,
	global_rank int unsigned null,
	country_rank int unsigned null,
	tscore bigint unsigned default 0 not null,
	rscore bigint unsigned default 0 not null,
	acc float(6,3) default 0.000 not null,
	plays int unsigned default 0 not null,
	playtime int unsigned default 0 not null,
	max_combo int unsigned default 0 not null,
	total_hits bigint unsigned default 0 not null,
	created_at datetime default current_timestamp not null,
	constraint stat_snapshots_user_mode_date_uindex
		unique (user_id, mode, snapshot_date)
);
create index stat_snapshots_mode_date_index
	on stat_snapshots (mode, snapshot_date);

# Prism data foundation (Track 3.3): persist the akatsuki_pp_py skill-component
# breakdown of `pp` (aim / speed / flashlight) that score submission already
# computes and then discarded. Nullable and additive -- older rows and modes the
# calculator does not break down simply leave them NULL. Nothing on the
# leaderboard hot path reads them, so they carry no index. Bundled into the same
# v5.3.2 block as the snapshots work: no DB has run v5.3.2 yet, so every live DB
# is still at <= 5.3.1 and will apply this alongside the snapshots table in one
# pass.
alter table scores
	add column pp_aim float(7,3) null,
	add column pp_speed float(7,3) null,
	add column pp_flashlight float(7,3) null;

# v5.3.3
# Prism anticheat: the staff review queue (Track 2.5). One row per flagged score
# -- the durable side of "flag, never auto-ban". The worker records the
# strongest signal and its evidence here for a human to action; re-analysis
# refreshes the detection columns but never the resolution columns, so a
# reviewer's decision is not clobbered and dismissed flags do not silently
# re-open. `user_id` is denormalised from scores so per-player views need no
# join. Its own version block (not bundled into v5.3.2) so a DB that has already
# applied the data-foundation migration still picks this up on the next run.
create table anticheat_flags
(
	score_id int not null,
	user_id int not null,
	mode tinyint(1) not null,
	status enum('open', 'reviewing', 'actioned', 'dismissed') default 'open' not null,
	severity varchar(16) not null,
	top_signal_code varchar(16) not null,
	top_signal_title varchar(128) not null,
	confidence float(7,6) default 0.000000 not null,
	triggered_count int default 0 not null,
	detail varchar(512) not null,
	evidence text null,
	first_flagged_at datetime default current_timestamp not null,
	last_flagged_at datetime default current_timestamp not null,
	resolved_by int null,
	resolved_at datetime null,
	resolution_note varchar(512) null,
	constraint anticheat_flags_score_id_uindex primary key (score_id)
);
create index anticheat_flags_status_index
	on anticheat_flags (status);
create index anticheat_flags_user_id_index
	on anticheat_flags (user_id);
create index anticheat_flags_mode_index
	on anticheat_flags (mode);

# v5.3.4
# Prism social: the activity feed (Track 4.1). An append-only log of notable
# player events (rank milestones, personal bests, new #1s, achievements) so a
# player and their friends can read back what happened -- stock bancho.py keeps
# none of this in a queryable form. `event_type` is an opaque slug and `data`
# is per-type JSON detail, so new event types need no schema change. Feeds page
# by id (keyset), never OFFSET, so a long log stays cheap to scroll. Its own
# version block so a DB already at 5.3.3 (the review-queue migration) still
# picks this up on the next run.
create table activity_events
(
	id bigint auto_increment
		primary key,
	user_id int not null,
	event_type varchar(32) not null,
	mode tinyint(1) null,
	data text null,
	created_at datetime default current_timestamp not null
);
create index activity_events_user_id_id_index
	on activity_events (user_id, id);
create index activity_events_event_type_index
	on activity_events (event_type);
create index activity_events_created_at_index
	on activity_events (created_at);

# v5.3.5
# Prism social: multiplayer match persistence (Track 4.5). Stock bancho.py holds
# every multiplayer match purely in memory (`app.state.sessions.matches`), so a
# restart erases all match history -- who hosted, what maps were played, when.
# `Match.id` there is a recycled 0-15 lobby *slot*, not a durable key, so these
# tables carry their own auto-increment ids and the in-memory match stashes the
# durable `mp_matches.id` on itself for the game rows to reference.
#
# Two tables, mirroring the in-memory shape:
#  - `mp_matches`: one row per lobby lifetime. Written when the lobby is created,
#    and `disbanded_at` is stamped when the last player leaves and the match is
#    torn down. `has_public_history` carries the //private privacy flag so a
#    private lobby's history stays private.
#  - `mp_match_games`: one row per *completed* game within a lobby (each map that
#    ran to MATCH_COMPLETE). A game that everyone quits mid-map is not recorded
#    -- an abandoned game is not history. Participants are stored as a JSON array
#    of user ids (like activity_events.data) rather than a third join table, with
#    a denormalised `participant_count` so "how many played" needs no parse. The
#    map/mode/mods/win-condition/team-type snapshot is copied at completion so
#    the record survives even after the lobby changes maps.
#
# As with the rest of the schema the foreign keys (host_id -> users,
# match_id -> mp_matches) are enforced in application logic, not by the DB, so a
# purged player or match orphans rather than cascades. Its own version block so a
# DB already at 5.3.4 (the activity-feed migration) still picks this up on the
# next run.
create table mp_matches
(
	id bigint auto_increment
		primary key,
	name varchar(50) not null,
	host_id int not null,
	has_public_history tinyint(1) default 1 not null,
	created_at datetime default current_timestamp not null,
	disbanded_at datetime null
);
create index mp_matches_host_id_index
	on mp_matches (host_id);
create index mp_matches_created_at_index
	on mp_matches (created_at);
create table mp_match_games
(
	id bigint auto_increment
		primary key,
	match_id bigint not null,
	map_md5 char(32) not null,
	map_id int default 0 not null,
	map_name varchar(256) default '' not null,
	mode tinyint(1) default 0 not null,
	mods int default 0 not null,
	win_condition tinyint(1) default 0 not null,
	team_type tinyint(1) default 0 not null,
	freemods tinyint(1) default 0 not null,
	scrim tinyint(1) default 0 not null,
	participant_count int default 0 not null,
	participants text null,
	started_at datetime default current_timestamp not null,
	ended_at datetime null
);
create index mp_match_games_match_id_id_index
	on mp_match_games (match_id, id);
create index mp_match_games_map_md5_index
	on mp_match_games (map_md5);
create index mp_match_games_started_at_index
	on mp_match_games (started_at);

# v5.3.6
# Prism social: per-player multiplayer scoreboards (Track 4.5, depth). v5.3.5
# recorded that a game happened and *who* was in it, but not *how they did* --
# osu! streams each player's live score to the server via MATCH_SCORE_UPDATE,
# but stock bancho.py only rebroadcasts those frames to the room and throws them
# away. This table keeps the final frame of each participant, so a completed
# game has an actual scoreboard: score, combo, accuracy components, grade
# inputs, mods, team, and whether they passed.
#
# One row per participant per game (child of mp_match_games). The score fields
# are the last MATCH_SCORE_UPDATE frame the server saw from that player before
# MATCH_COMPLETE, captured cheaply on the hot path (the raw frame is stashed on
# the slot, unparsed) and decoded once at completion off the packet handler.
# `passed` is false for a player who signalled MATCH_FAILED during the game.
# `placement` is the 1-based rank within the game under its win condition,
# computed at write time so a scoreboard needs no re-sort on read.
#
# As elsewhere the foreign keys (game_id -> mp_match_games, user_id -> users)
# are enforced in application logic, not the DB. Its own version block so a DB
# already at 5.3.5 still picks this up on the next run.
create table mp_match_game_scores
(
	id bigint auto_increment
		primary key,
	game_id bigint not null,
	user_id int not null,
	team tinyint(1) default 0 not null,
	mods int default 0 not null,
	score int default 0 not null,
	max_combo int default 0 not null,
	num300 int default 0 not null,
	num100 int default 0 not null,
	num50 int default 0 not null,
	num_geki int default 0 not null,
	num_katu int default 0 not null,
	num_miss int default 0 not null,
	acc float(6,3) default 0.000 not null,
	perfect tinyint(1) default 0 not null,
	passed tinyint(1) default 1 not null,
	placement int default 0 not null,
	created_at datetime default current_timestamp not null
);
create index mp_match_game_scores_game_id_placement_index
	on mp_match_game_scores (game_id, placement);
create index mp_match_game_scores_user_id_index
	on mp_match_game_scores (user_id);

# v5.3.7
# Prism social: durable spectator-session history (Track 4). osu! spectating is
# a purely in-memory relationship in stock bancho.py -- `Player.spectators` and
# `Player.spectating` (app/objects/player.py) live only for the length of a
# connection and are gone on logout or restart, so "who watched whom, and for
# how long" has no record. This table is that record: one row per spectate
# session, opened when a viewer starts spectating a host and closed when they
# stop (or log out, which stops it for them).
#
# `host_id` is the player being watched, `spectator_id` the one watching;
# `started_at` is stamped at START_SPECTATING, `ended_at` at STOP_SPECTATING /
# logout. A row with a null `ended_at` is a session that was still open when the
# server last ran -- like `mp_matches.disbanded_at`, it is simply never stamped
# if the process died mid-session rather than being back-filled.
#
# As elsewhere the foreign keys (host_id -> users, spectator_id -> users) are
# enforced in application logic, not the DB, so a purged player orphans rather
# than cascades. Its own version block so a DB already at 5.3.6 picks this up on
# the next run.
create table spectator_sessions
(
	id bigint auto_increment
		primary key,
	host_id int not null,
	spectator_id int not null,
	started_at datetime default current_timestamp not null,
	ended_at datetime null
);
create index spectator_sessions_host_id_id_index
	on spectator_sessions (host_id, id);
create index spectator_sessions_spectator_id_id_index
	on spectator_sessions (spectator_id, id);
create index spectator_sessions_started_at_index
	on spectator_sessions (started_at);

# v5.3.8
# Prism social: Discord account links (Track 4). Ties an osu! account on this
# server to a Discord account the player proved they own through Discord's OAuth2
# flow, so the two identities can be surfaced together (and a Discord bot can map
# either direction). One row per link: `user_id` is the primary key, so a player
# has at most one Discord account linked; `discord_id` carries a unique index, so
# a Discord account backs at most one player -- a second attempt to claim an
# already-linked Discord is refused by the service, never silently reassigned.
#
# As elsewhere the foreign key (`user_id` -> users) is enforced in application
# logic, not the DB, so a purged player orphans rather than cascades. Its own
# version block so a DB already at 5.3.7 picks this up on the next run.
create table user_discord_links
(
	user_id int not null
		primary key,
	discord_id varchar(20) not null,
	discord_username varchar(32) not null,
	linked_at datetime default current_timestamp not null
);
create unique index user_discord_links_discord_id_uindex
	on user_discord_links (discord_id);
