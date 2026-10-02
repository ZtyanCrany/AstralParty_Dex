
protocol/interior.protoprotocolmodel/model.protoerrcode/code.proto"	
Offline"
Online"
DelayOffLine"D
EnterGameReq
randKey (	#
account (2.model.AccountInfo"m
EnterGameResp
code (2
.code.Code#
account (2.model.AccountInfo
player (2.model.Player"
	NetStoped"

GameClosed"

RoomPing"…
SysStartMatchReq
teamId (
mode (2.model.MatchMode
mapId (

difficulty (
players (2.model.Player"=
SysStartMatchResp
code (2
.code.Code
teamId ("2
SysCancelMatchReq
teamId (
count (">
SysCancelMatchResp
code (2
.code.Code
teamId ("'
SysGetRandomMatchTeamReq
num ("E
SysGetRandomMatchTeamResp(
teams (2.protocol.RandomMatchTeam"=
RandomMatchTeam

id (
players (2.model.Player"—
SysSyncGuildMember
playerId (
weeklyActivity (
lastLoginTime (
weeklySignIn (
lastLogoutTime (

playerName (	"C
SysProcessGuildApplicationC2S
guildId (
	guildName (	"3
SysProcessGuildApplicationS2C

playerName (	"?
SysSendGuildInvitationC2S
guildId (
	inviterId ("
SysSendGuildInvitationS2C"m
SysExitGuildC2S
guildId (
	guildName (	
playerId (

playerName (	
exitType ("
SysExitGuildS2C"˜
SysUpdateGuildMissionC2SH
missionCond (23.protocol.SysUpdateGuildMissionC2S.MissionCondEntry2
MissionCondEntry
key (
value (:8":
SysSendGuildChatMsgC2S 
msg (2.model.GuildChatMsg"
AutoDisbandGuild"&
SysDisbandGuildC2S
exitType ("
SysGmGuildSearchC2S"2
SysGmGuildSearchS2C
guild (2.model.Guild"'
SysGmGuildChangeNameC2S
name (	"
SysGmGuildDisbandC2S"%
SysGmGuildFreezeC2S
frozen ("(
SysGmGuildMuteC2S
muteEndTime ("(
SysGmGuildCreateBanC2S
banned (".
SysGmGuildMutePlayerC2S
muteEndTime (B$Zparty/pb/protocolªparty.protocolbproto3