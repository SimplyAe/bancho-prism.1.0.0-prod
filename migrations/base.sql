create table achievements
(
	id int auto_increment
		primary key,
	file varchar(128) not null,
	name varchar(128) charset utf8 not null,
	`desc` varchar(256) charset utf8 not null,
	cond varchar(64) not null,
	constraint achievements_desc_uindex
		unique (`desc`),
	constraint achievements_file_uindex
		unique (file),
	constraint achievements_name_uindex
		unique (name)
);

create table channels
(
	id int auto_increment
		primary key,
	name varchar(32) not null,
	topic varchar(256) not null,
	read_priv int default 1 not null,
	write_priv int default 2 not null,
	auto_join tinyint(1) default 0 not null,
	constraint channels_name_uindex
		unique (name)
);
create index channels_auto_join_index
	on channels (auto_join);

create table clans
(
	id int auto_increment
		primary key,
	name varchar(16) charset utf8 not null,
	tag varchar(6) charset utf8 not null,
	owner int not null,
	created_at datetime not null,
	constraint clans_name_uindex
		unique (name),
	constraint clans_owner_uindex
		unique (owner),
	constraint clans_tag_uindex
		unique (tag)
);

create table client_hashes
(
	userid int not null,
	osupath char(32) not null,
	adapters char(32) not null,
	uninstall_id char(32) not null,
	disk_serial char(32) not null,
	latest_time datetime not null,
	occurrences int default 0 not null,
	primary key (userid, osupath, adapters, uninstall_id, disk_serial)
);

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

create table favourites
(
	userid int not null,
	setid int not null,
	created_at int default 0 not null,
	primary key (userid, setid)
);

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

create table relationships
(
	user1 int not null,
	user2 int not null,
	type enum('friend', 'block') not null,
	primary key (user1, user2)
);

create table logs
(
	id int auto_increment
		primary key,
	`from` int not null comment 'both from and to are playerids',
	`to` int not null,
	`action` varchar(32) not null,
	msg varchar(2048) charset utf8 null,
	time datetime not null on update CURRENT_TIMESTAMP
);

create table mail
(
	id int auto_increment
		primary key,
	from_id int not null,
	to_id int not null,
	msg varchar(2048) charset utf8 not null,
	time int null,
	`read` tinyint(1) default 0 not null
);

create table maps
(
	server enum('osu!', 'private') default 'osu!' not null,
	id int not null,
	set_id int not null,
	status int not null,
	md5 char(32) not null,
	artist varchar(128) charset utf8 not null,
	title varchar(128) charset utf8 not null,
	version varchar(128) charset utf8 not null,
	creator varchar(32) charset utf8 not null,
	filename varchar(256) charset utf8 not null,
	last_update datetime not null,
	total_length int not null,
	max_combo int not null,
	frozen tinyint(1) default 0 not null,
	plays int default 0 not null,
	passes int default 0 not null,
	mode tinyint(1) default 0 not null,
	bpm float(12,2) default 0.00 not null,
	cs float(4,2) default 0.00 not null,
	ar float(4,2) default 0.00 not null,
	od float(4,2) default 0.00 not null,
	hp float(4,2) default 0.00 not null,
	diff float(6,3) default 0.000 not null,
	primary key (server, id),
	constraint maps_id_uindex
		unique (id),
	constraint maps_md5_uindex
		unique (md5)
);
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

create table mapsets
(
	server enum('osu!', 'private') default 'osu!' not null,
	id int not null,
	last_osuapi_check datetime default CURRENT_TIMESTAMP not null,
	primary key (server, id),
	constraint nmapsets_id_uindex
		unique (id)
);

# id allocation for privately-hosted maps and sets (beatmap submission).
# Seeded far above osu!'s id space so a custom id can never collide with a real
# beatmap id -- `maps.id`/`maps.md5` are independently unique and the osu!api
# refresh writes with REPLACE INTO, so a collision destroys a row rather than
# raising. See the v5.3.9 block in migrations.sql for the full reasoning.
create table map_id_sequence
(
	id int auto_increment
		primary key,
	kind enum('map', 'set') not null,
	allocated_by int not null,
	allocated_at datetime default current_timestamp not null
);

# One row per submitted set, holding the moderation state `maps` has no room for.
create table map_submissions
(
	set_id int not null
		primary key,
	submitter_user_id int not null,
	review_state enum('pending', 'approved', 'rejected') default 'pending' not null,
	declared_creator varchar(64) charset utf8 not null,
	difficulty_count int not null,
	osz_size_bytes int not null,
	osz_sha256 char(64) not null,
	submitted_at datetime default current_timestamp not null,
	updated_at datetime default current_timestamp not null,
	reviewed_by int null,
	reviewed_at datetime null,
	review_note varchar(512) charset utf8 null
);
create index map_submissions_submitter_user_id_index
	on map_submissions (submitter_user_id);
create index map_submissions_review_state_index
	on map_submissions (review_state);

create table map_requests
(
	id int auto_increment
		primary key,
	map_id int not null,
	player_id int not null,
	datetime datetime not null,
	active tinyint(1) not null
);

create table performance_reports
(
	scoreid bigint(20) unsigned not null,
	mod_mode enum('vanilla', 'relax', 'autopilot') default 'vanilla' not null,
	os varchar(64) not null,
	fullscreen tinyint(1) not null,
	fps_cap varchar(16) not null,
	compatibility tinyint(1) not null,
	version varchar(16) not null,
	start_time int not null,
	end_time int not null,
	frame_count int not null,
	spike_frames int not null,
	aim_rate int not null,
	completion tinyint(1) not null,
	identifier varchar(128) null comment 'really don''t know much about this yet',
	average_frametime int not null,
	primary key (scoreid, mod_mode)
);

create table ratings
(
	userid int not null,
	map_md5 char(32) not null,
	rating tinyint(2) not null,
	primary key (userid, map_md5)
);

create table scores
(
	id bigint unsigned auto_increment
		primary key,
	map_md5 char(32) not null,
	score int not null,
	pp float(7,3) not null,
	acc float(6,3) not null,
	max_combo int not null,
	mods int not null,
	n300 int not null,
	n100 int not null,
	n50 int not null,
	nmiss int not null,
	ngeki int not null,
	nkatu int not null,
	grade varchar(2) default 'N' not null,
	status tinyint not null,
	mode tinyint not null,
	play_time datetime not null,
	time_elapsed int not null,
	client_flags int not null,
	userid int not null,
	perfect tinyint(1) not null,
	online_checksum char(32) not null,
	pp_aim float(7,3) null,
	pp_speed float(7,3) null,
	pp_flashlight float(7,3) null
);
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
create index scores_fetch_leaderboard_generic_index
	on scores (map_md5, status, mode);

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

# Prism anticheat: the staff review queue (Track 2.5). One row per flagged score
# -- the durable side of "flag, never auto-ban". The worker records the
# strongest signal and its evidence here for a human to action; re-analysis
# refreshes the detection columns but never the resolution columns, so a
# reviewer's decision is not clobbered and dismissed flags do not silently
# re-open. `user_id` is denormalised from scores so per-player views need no join.
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

create table startups
(
	id int auto_increment
		primary key,
	ver_major tinyint not null,
	ver_minor tinyint not null,
	ver_micro tinyint not null,
	datetime datetime not null
);

# Prism data foundation: daily per-player, per-mode stat snapshots (Track 3.1).
# One immutable row per (user, mode, day) preserving the numbers as they stood
# that day. The live `stats` table only ever holds the current value, so rank
# history, peak rank, and the anticheat behavioural baselines -- all of which
# need the past -- would be unreconstructable without this. Ranks are captured
# from `stats` with a window function over the ranked predicate, not read live
# from Redis, so a snapshot is durable even with a cold leaderboard.
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

# Prism social: the activity feed (Track 4.1). An append-only log of notable
# player events (rank milestones, personal bests, new #1s, achievements) that a
# player and their friends can read back -- stock bancho.py records none of this
# in a queryable form. `event_type` is an opaque slug and `data` is per-type
# JSON detail, so a new event type needs no schema change. Feeds page by id
# (keyset), never OFFSET, so a long log stays cheap to scroll.
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

create table stats
(
	id int auto_increment,
	mode tinyint(1) not null,
	tscore bigint unsigned default 0 not null,
	rscore bigint unsigned default 0 not null,
	pp int unsigned default 0 not null,
	plays int unsigned default 0 not null,
	playtime int unsigned default 0 not null,
	acc float(6,3) default 0.000 not null,
	max_combo int unsigned default 0 not null,
	total_hits int unsigned default 0 not null,
	replay_views int unsigned default 0 not null,
	xh_count int unsigned default 0 not null,
	x_count int unsigned default 0 not null,
	sh_count int unsigned default 0 not null,
	s_count int unsigned default 0 not null,
	a_count int unsigned default 0 not null,
	primary key (id, mode)
);
create index stats_mode_index
	on stats (mode);
create index stats_pp_index
	on stats (pp);
create index stats_tscore_index
	on stats (tscore);
create index stats_rscore_index
	on stats (rscore);

create table tourney_pool_maps
(
	map_id int not null,
	pool_id int not null,
	mods int not null,
	slot tinyint not null,
	primary key (map_id, pool_id)
);
create index tourney_pool_maps_mods_slot_index
	on tourney_pool_maps (mods, slot);
create index tourney_pool_maps_tourney_pools_id_fk
	on tourney_pool_maps (pool_id);

create table tourney_pools
(
	id int auto_increment
		primary key,
	name varchar(16) not null,
	created_at datetime not null,
	created_by int not null
);

create index tourney_pools_users_id_fk
	on tourney_pools (created_by);

# Prism social: Discord account links (Track 4). Ties an osu! account on this
# server to a Discord account the player proved they own via OAuth2, so the two
# identities can be shown together and a Discord bot can map either way. One row
# per link: `user_id` is the PK, so a player has at most one Discord linked, and
# `discord_id` is unique, so a Discord account can back at most one player -- the
# service refuses a second claim rather than silently stealing the link. As with
# the rest of the schema the foreign key (`user_id` -> users) is enforced in
# application logic, not the DB, so a purged player orphans rather than cascades.
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

create table user_achievements
(
	userid int not null,
	achid int not null,
	primary key (userid, achid)
);
create index user_achievements_achid_index
	on user_achievements (achid);
create index user_achievements_userid_index
	on user_achievements (userid);

create table users
(
	id int auto_increment
		primary key,
	name varchar(32) charset utf8 not null,
	safe_name varchar(32) charset utf8 not null,
	email varchar(254) not null,
	priv int default 1 not null,
	pw_bcrypt char(60) not null,
	country char(2) default 'xx' not null,
	silence_end int default 0 not null,
	donor_end int default 0 not null,
	creation_time int default 0 not null,
	latest_activity int default 0 not null,
	clan_id int default 0 not null,
	clan_priv tinyint(1) default 0 not null,
	preferred_mode int default 0 not null,
	play_style int default 0 not null,
	custom_badge_name varchar(16) charset utf8 null,
	custom_badge_icon varchar(64) null,
	userpage_content varchar(2048) charset utf8 null,
	api_key char(36) null,
	constraint users_api_key_uindex
		unique (api_key),
	constraint users_email_uindex
		unique (email),
	constraint users_name_uindex
		unique (name),
	constraint users_safe_name_uindex
		unique (safe_name)
);
create index users_priv_index
	on users (priv);
create index users_clan_id_index
	on users (clan_id);
create index users_clan_priv_index
	on users (clan_priv);
create index users_country_index
	on users (country);

insert into users (id, name, safe_name, priv, country, silence_end, email, pw_bcrypt, creation_time, latest_activity)
values (1, 'BanchoBot', 'banchobot', 1, 'ca', 0, 'bot@akatsuki.pw',
        '_______________________my_cool_bcrypt_______________________', UNIX_TIMESTAMP(), UNIX_TIMESTAMP());

INSERT INTO stats (id, mode) VALUES (1, 0); # vn!std
INSERT INTO stats (id, mode) VALUES (1, 1); # vn!taiko
INSERT INTO stats (id, mode) VALUES (1, 2); # vn!catch
INSERT INTO stats (id, mode) VALUES (1, 3); # vn!mania
INSERT INTO stats (id, mode) VALUES (1, 4); # rx!std
INSERT INTO stats (id, mode) VALUES (1, 5); # rx!taiko
INSERT INTO stats (id, mode) VALUES (1, 6); # rx!catch
INSERT INTO stats (id, mode) VALUES (1, 8); # ap!std


# userid 2 is reserved for ppy in osu!, and the
# client will not allow users to pm this id.
# If you want this, simply remove these two lines.
alter table users auto_increment = 3;
alter table stats auto_increment = 3;

# privately-hosted beatmap ids start far above osu!'s id space (~5M today) while
# staying inside signed int32, so a custom id can never collide with a real one.
# Losing this seed (e.g. restoring a data-only dump) would allocate from 1 and
# let a REPLACE destroy a real beatmap row, so the repository asserts the floor
# on every allocation instead of trusting it.
alter table map_id_sequence auto_increment = 2000000000;

insert into channels (name, topic, read_priv, write_priv, auto_join)
values ('#osu', 'General discussion.', 1, 2, true),
	   ('#announce', 'Exemplary performance and public announcements.', 1, 24576, true),
	   ('#lobby', 'Multiplayer lobby discussion room.', 1, 2, false),
	   ('#supporter', 'General discussion for supporters.', 48, 48, false),
	   ('#staff', 'General discussion for staff members.', 28672, 28672, true),
	   ('#admin', 'General discussion for administrators.', 24576, 24576, true),
	   ('#dev', 'General discussion for developers.', 16384, 16384, true);

insert into achievements (id, file, name, `desc`, cond) values (1, 'osu-skill-pass-1', 'Rising Star', 'Can''t go forward without the first steps.', '(score.mods & 1 == 0) and 1 <= score.sr < 2 and mode_vn == 0');
insert into achievements (id, file, name, `desc`, cond) values (2, 'osu-skill-pass-2', 'Constellation Prize', 'Definitely not a consolation prize. Now things start getting hard!', '(score.mods & 1 == 0) and 2 <= score.sr < 3 and mode_vn == 0');
insert into achievements (id, file, name, `desc`, cond) values (3, 'osu-skill-pass-3', 'Building Confidence', 'Oh, you''ve SO got this.', '(score.mods & 1 == 0) and 3 <= score.sr < 4 and mode_vn == 0');
insert into achievements (id, file, name, `desc`, cond) values (4, 'osu-skill-pass-4', 'Insanity Approaches', 'You''re not twitching, you''re just ready.', '(score.mods & 1 == 0) and 4 <= score.sr < 5 and mode_vn == 0');
insert into achievements (id, file, name, `desc`, cond) values (5, 'osu-skill-pass-5', 'These Clarion Skies', 'Everything seems so clear now.', '(score.mods & 1 == 0) and 5 <= score.sr < 6 and mode_vn == 0');
insert into achievements (id, file, name, `desc`, cond) values (6, 'osu-skill-pass-6', 'Above and Beyond', 'A cut above the rest.', '(score.mods & 1 == 0) and 6 <= score.sr < 7 and mode_vn == 0');
insert into achievements (id, file, name, `desc`, cond) values (7, 'osu-skill-pass-7', 'Supremacy', 'All marvel before your prowess.', '(score.mods & 1 == 0) and 7 <= score.sr < 8 and mode_vn == 0');
insert into achievements (id, file, name, `desc`, cond) values (8, 'osu-skill-pass-8', 'Absolution', 'My god, you''re full of stars!', '(score.mods & 1 == 0) and 8 <= score.sr < 9 and mode_vn == 0');
insert into achievements (id, file, name, `desc`, cond) values (9, 'osu-skill-pass-9', 'Event Horizon', 'No force dares to pull you under.', '(score.mods & 1 == 0) and 9 <= score.sr < 10 and mode_vn == 0');
insert into achievements (id, file, name, `desc`, cond) values (10, 'osu-skill-pass-10', 'Phantasm', 'Fevered is your passion, extraordinary is your skill.', '(score.mods & 1 == 0) and 10 <= score.sr < 11 and mode_vn == 0');
insert into achievements (id, file, name, `desc`, cond) values (11, 'osu-skill-fc-1', 'Totality', 'All the notes. Every single one.', 'score.perfect and 1 <= score.sr < 2 and mode_vn == 0');
insert into achievements (id, file, name, `desc`, cond) values (12, 'osu-skill-fc-2', 'Business As Usual', 'Two to go, please.', 'score.perfect and 2 <= score.sr < 3 and mode_vn == 0');
insert into achievements (id, file, name, `desc`, cond) values (13, 'osu-skill-fc-3', 'Building Steam', 'Hey, this isn''t so bad.', 'score.perfect and 3 <= score.sr < 4 and mode_vn == 0');
insert into achievements (id, file, name, `desc`, cond) values (14, 'osu-skill-fc-4', 'Moving Forward', 'Bet you feel good about that.', 'score.perfect and 4 <= score.sr < 5 and mode_vn == 0');
insert into achievements (id, file, name, `desc`, cond) values (15, 'osu-skill-fc-5', 'Paradigm Shift', 'Surprisingly difficult.', 'score.perfect and 5 <= score.sr < 6 and mode_vn == 0');
insert into achievements (id, file, name, `desc`, cond) values (16, 'osu-skill-fc-6', 'Anguish Quelled', 'Don''t choke.', 'score.perfect and 6 <= score.sr < 7 and mode_vn == 0');
insert into achievements (id, file, name, `desc`, cond) values (17, 'osu-skill-fc-7', 'Never Give Up', 'Excellence is its own reward.', 'score.perfect and 7 <= score.sr < 8 and mode_vn == 0');
insert into achievements (id, file, name, `desc`, cond) values (18, 'osu-skill-fc-8', 'Aberration', 'They said it couldn''t be done. They were wrong.', 'score.perfect and 8 <= score.sr < 9 and mode_vn == 0');
insert into achievements (id, file, name, `desc`, cond) values (19, 'osu-skill-fc-9', 'Chosen', 'Reign among the Prometheans, where you belong.', 'score.perfect and 9 <= score.sr < 10 and mode_vn == 0');
insert into achievements (id, file, name, `desc`, cond) values (20, 'osu-skill-fc-10', 'Unfathomable', 'You have no equal.', 'score.perfect and 10 <= score.sr < 11 and mode_vn == 0');
insert into achievements (id, file, name, `desc`, cond) values (21, 'osu-combo-500', '500 Combo', '500 big ones! You''re moving up in the world!', '500 <= score.max_combo < 750 and mode_vn == 0');
insert into achievements (id, file, name, `desc`, cond) values (22, 'osu-combo-750', '750 Combo', '750 notes back to back? Woah.', '750 <= score.max_combo < 1000 and mode_vn == 0');
insert into achievements (id, file, name, `desc`, cond) values (23, 'osu-combo-1000', '1000 Combo', 'A thousand reasons why you rock at this game.', '1000 <= score.max_combo < 2000 and mode_vn == 0');
insert into achievements (id, file, name, `desc`, cond) values (24, 'osu-combo-2000', '2000 Combo', 'Nothing can stop you now.', '2000 <= score.max_combo and mode_vn == 0');
insert into achievements (id, file, name, `desc`, cond) values (25, 'taiko-skill-pass-1', 'My First Don', 'Marching to the beat of your own drum. Literally.', '(score.mods & 1 == 0) and 1 <= score.sr < 2 and mode_vn == 1');
insert into achievements (id, file, name, `desc`, cond) values (26, 'taiko-skill-pass-2', 'Katsu Katsu Katsu', 'Hora! Izuko!', '(score.mods & 1 == 0) and 2 <= score.sr < 3 and mode_vn == 1');
insert into achievements (id, file, name, `desc`, cond) values (27, 'taiko-skill-pass-3', 'Not Even Trying', 'Muzukashii? Not even.', '(score.mods & 1 == 0) and 3 <= score.sr < 4 and mode_vn == 1');
insert into achievements (id, file, name, `desc`, cond) values (28, 'taiko-skill-pass-4', 'Face Your Demons', 'The first trials are now behind you, but are you a match for the Oni?', '(score.mods & 1 == 0) and 4 <= score.sr < 5 and mode_vn == 1');
insert into achievements (id, file, name, `desc`, cond) values (29, 'taiko-skill-pass-5', 'The Demon Within', 'No rest for the wicked.', '(score.mods & 1 == 0) and 5 <= score.sr < 6 and mode_vn == 1');
insert into achievements (id, file, name, `desc`, cond) values (30, 'taiko-skill-pass-6', 'Drumbreaker', 'Too strong.', '(score.mods & 1 == 0) and 6 <= score.sr < 7 and mode_vn == 1');
insert into achievements (id, file, name, `desc`, cond) values (31, 'taiko-skill-pass-7', 'The Godfather', 'You are the Don of Dons.', '(score.mods & 1 == 0) and 7 <= score.sr < 8 and mode_vn == 1');
insert into achievements (id, file, name, `desc`, cond) values (32, 'taiko-skill-pass-8', 'Rhythm Incarnate', 'Feel the beat. Become the beat.', '(score.mods & 1 == 0) and 8 <= score.sr < 9 and mode_vn == 1');
insert into achievements (id, file, name, `desc`, cond) values (33, 'taiko-skill-fc-1', 'Keeping Time', 'Don, then katsu. Don, then katsu..', 'score.perfect and 1 <= score.sr < 2 and mode_vn == 1');
insert into achievements (id, file, name, `desc`, cond) values (34, 'taiko-skill-fc-2', 'To Your Own Beat', 'Straight and steady.', 'score.perfect and 2 <= score.sr < 3 and mode_vn == 1');
insert into achievements (id, file, name, `desc`, cond) values (35, 'taiko-skill-fc-3', 'Big Drums', 'Bigger scores to match.', 'score.perfect and 3 <= score.sr < 4 and mode_vn == 1');
insert into achievements (id, file, name, `desc`, cond) values (36, 'taiko-skill-fc-4', 'Adversity Overcome', 'Difficult? Not for you.', 'score.perfect and 4 <= score.sr < 5 and mode_vn == 1');
insert into achievements (id, file, name, `desc`, cond) values (37, 'taiko-skill-fc-5', 'Demonslayer', 'An Oni felled forevermore.', 'score.perfect and 5 <= score.sr < 6 and mode_vn == 1');
insert into achievements (id, file, name, `desc`, cond) values (38, 'taiko-skill-fc-6', 'Rhythm''s Call', 'Heralding true skill.', 'score.perfect and 6 <= score.sr < 7 and mode_vn == 1');
insert into achievements (id, file, name, `desc`, cond) values (39, 'taiko-skill-fc-7', 'Time Everlasting', 'Not a single beat escapes you.', 'score.perfect and 7 <= score.sr < 8 and mode_vn == 1');
insert into achievements (id, file, name, `desc`, cond) values (40, 'taiko-skill-fc-8', 'The Drummer''s Throne', 'Percussive brilliance befitting royalty alone.', 'score.perfect and 8 <= score.sr < 9 and mode_vn == 1');
insert into achievements (id, file, name, `desc`, cond) values (41, 'fruits-skill-pass-1', 'A Slice Of Life', 'Hey, this fruit catching business isn''t bad.', '(score.mods & 1 == 0) and 1 <= score.sr < 2 and mode_vn == 2');
insert into achievements (id, file, name, `desc`, cond) values (42, 'fruits-skill-pass-2', 'Dashing Ever Forward', 'Fast is how you do it.', '(score.mods & 1 == 0) and 2 <= score.sr < 3 and mode_vn == 2');
insert into achievements (id, file, name, `desc`, cond) values (43, 'fruits-skill-pass-3', 'Zesty Disposition', 'No scurvy for you, not with that much fruit.', '(score.mods & 1 == 0) and 3 <= score.sr < 4 and mode_vn == 2');
insert into achievements (id, file, name, `desc`, cond) values (44, 'fruits-skill-pass-4', 'Hyperdash ON!', 'Time and distance is no obstacle to you.', '(score.mods & 1 == 0) and 4 <= score.sr < 5 and mode_vn == 2');
insert into achievements (id, file, name, `desc`, cond) values (45, 'fruits-skill-pass-5', 'It''s Raining Fruit', 'And you can catch them all.', '(score.mods & 1 == 0) and 5 <= score.sr < 6 and mode_vn == 2');
insert into achievements (id, file, name, `desc`, cond) values (46, 'fruits-skill-pass-6', 'Fruit Ninja', 'Legendary techniques.', '(score.mods & 1 == 0) and 6 <= score.sr < 7 and mode_vn == 2');
insert into achievements (id, file, name, `desc`, cond) values (47, 'fruits-skill-pass-7', 'Dreamcatcher', 'No fruit, only dreams now.', '(score.mods & 1 == 0) and 7 <= score.sr < 8 and mode_vn == 2');
insert into achievements (id, file, name, `desc`, cond) values (48, 'fruits-skill-pass-8', 'Lord of the Catch', 'Your kingdom kneels before you.', '(score.mods & 1 == 0) and 8 <= score.sr < 9 and mode_vn == 2');
insert into achievements (id, file, name, `desc`, cond) values (49, 'fruits-skill-fc-1', 'Sweet And Sour', 'Apples and oranges, literally.', 'score.perfect and 1 <= score.sr < 2 and mode_vn == 2');
insert into achievements (id, file, name, `desc`, cond) values (50, 'fruits-skill-fc-2', 'Reaching The Core', 'The seeds of future success.', 'score.perfect and 2 <= score.sr < 3 and mode_vn == 2');
insert into achievements (id, file, name, `desc`, cond) values (51, 'fruits-skill-fc-3', 'Clean Platter', 'Clean only of failure. It is completely full, otherwise.', 'score.perfect and 3 <= score.sr < 4 and mode_vn == 2');
insert into achievements (id, file, name, `desc`, cond) values (52, 'fruits-skill-fc-4', 'Between The Rain', 'No umbrella needed.', 'score.perfect and 4 <= score.sr < 5 and mode_vn == 2');
insert into achievements (id, file, name, `desc`, cond) values (53, 'fruits-skill-fc-5', 'Addicted', 'That was an overdose?', 'score.perfect and 5 <= score.sr < 6 and mode_vn == 2');
insert into achievements (id, file, name, `desc`, cond) values (54, 'fruits-skill-fc-6', 'Quickening', 'A dash above normal limits.', 'score.perfect and 6 <= score.sr < 7 and mode_vn == 2');
insert into achievements (id, file, name, `desc`, cond) values (55, 'fruits-skill-fc-7', 'Supersonic', 'Faster than is reasonably necessary.', 'score.perfect and 7 <= score.sr < 8 and mode_vn == 2');
insert into achievements (id, file, name, `desc`, cond) values (56, 'fruits-skill-fc-8', 'Dashing Scarlet', 'Speed beyond mortal reckoning.', 'score.perfect and 8 <= score.sr < 9 and mode_vn == 2');
insert into achievements (id, file, name, `desc`, cond) values (57, 'mania-skill-pass-1', 'First Steps', 'It isn''t 9-to-5, but 1-to-9. Keys, that is.', '(score.mods & 1 == 0) and 1 <= score.sr < 2 and mode_vn == 3');
insert into achievements (id, file, name, `desc`, cond) values (58, 'mania-skill-pass-2', 'No Normal Player', 'Not anymore, at least.', '(score.mods & 1 == 0) and 2 <= score.sr < 3 and mode_vn == 3');
insert into achievements (id, file, name, `desc`, cond) values (59, 'mania-skill-pass-3', 'Impulse Drive', 'Not quite hyperspeed, but getting close.', '(score.mods & 1 == 0) and 3 <= score.sr < 4 and mode_vn == 3');
insert into achievements (id, file, name, `desc`, cond) values (60, 'mania-skill-pass-4', 'Hyperspeed', 'Woah.', '(score.mods & 1 == 0) and 4 <= score.sr < 5 and mode_vn == 3');
insert into achievements (id, file, name, `desc`, cond) values (61, 'mania-skill-pass-5', 'Ever Onwards', 'Another challenge is just around the corner.', '(score.mods & 1 == 0) and 5 <= score.sr < 6 and mode_vn == 3');
insert into achievements (id, file, name, `desc`, cond) values (62, 'mania-skill-pass-6', 'Another Surpassed', 'Is there no limit to your skills?', '(score.mods & 1 == 0) and 6 <= score.sr < 7 and mode_vn == 3');
insert into achievements (id, file, name, `desc`, cond) values (63, 'mania-skill-pass-7', 'Extra Credit', 'See me after class.', '(score.mods & 1 == 0) and 7 <= score.sr < 8 and mode_vn == 3');
insert into achievements (id, file, name, `desc`, cond) values (64, 'mania-skill-pass-8', 'Maniac', 'There''s just no stopping you.', '(score.mods & 1 == 0) and 8 <= score.sr < 9 and mode_vn == 3');
insert into achievements (id, file, name, `desc`, cond) values (65, 'mania-skill-fc-1', 'Keystruck', 'The beginning of a new story', 'score.perfect and 1 <= score.sr < 2 and mode_vn == 3');
insert into achievements (id, file, name, `desc`, cond) values (66, 'mania-skill-fc-2', 'Keying In', 'Finding your groove.', 'score.perfect and 2 <= score.sr < 3 and mode_vn == 3');
insert into achievements (id, file, name, `desc`, cond) values (67, 'mania-skill-fc-3', 'Hyperflow', 'You can *feel* the rhythm.', 'score.perfect and 3 <= score.sr < 4 and mode_vn == 3');
insert into achievements (id, file, name, `desc`, cond) values (68, 'mania-skill-fc-4', 'Breakthrough', 'Many skills mastered, rolled into one.', 'score.perfect and 4 <= score.sr < 5 and mode_vn == 3');
insert into achievements (id, file, name, `desc`, cond) values (69, 'mania-skill-fc-5', 'Everything Extra', 'Giving your all is giving everything you have.', 'score.perfect and 5 <= score.sr < 6 and mode_vn == 3');
insert into achievements (id, file, name, `desc`, cond) values (70, 'mania-skill-fc-6', 'Level Breaker', 'Finesse beyond reason', 'score.perfect and 6 <= score.sr < 7 and mode_vn == 3');
insert into achievements (id, file, name, `desc`, cond) values (71, 'mania-skill-fc-7', 'Step Up', 'A precipice rarely seen.', 'score.perfect and 7 <= score.sr < 8 and mode_vn == 3');
insert into achievements (id, file, name, `desc`, cond) values (72, 'mania-skill-fc-8', 'Behind The Veil', 'Supernatural!', 'score.perfect and 8 <= score.sr < 9 and mode_vn == 3');
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
