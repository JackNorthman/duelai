# ClaudeCommander.py - lets Claude (the AI assistant) command ships in
# QuickBattle, live, from outside the game.  v3: A WHOLE FLEET AT ONCE.
#
# QuickBattle only.  The "Duel Claude" button on the TACTICAL menu now
# establishes the COMMAND LINK (1 Hz tick loop + telemetry + order polling)
# without spawning anything.  Every ship - including a classic 1v1 duel
# opponent - arrives via a SPAWN order from outside.  Claude can field up to
# MAX_FLEET ships on EITHER side:
#
#   side 0 (enemy)     added to the mission Enemy group, fights the player
#                      (the Borg collective scenario)
#   side 1 (friendly)  added to the mission Friendly group, fights whatever
#                      the player is fighting (the DS9 gauntlet scenario)
#
# Hostility in this engine is nothing but ObjectGroup membership (verified
# 2026-08-04: no diplomacy API exists; AIs re-read their target group every
# ~5 s via a live pointer - Preprocessors.py SelectTarget; HUD colour is the
# same membership resolved in C++), so spawn-and-AddName is all it takes.
#
# File bridge (engine config API - Python open() is dead in this build,
# see v2 notes below):
#
#   claude_state.cfg   game saves every second: [ClaudeState], fixed-arity
#                      slot blocks (see telemetry key list at BuildState).
#                      Ints via SetIntValue ("Key|123"), strings via
#                      SetStringValue ("Key=text") - both proven stock calls.
#   claude_orders.cfg  Claude writes, game polls via LoadConfigFile:
#                      [ClaudeOrders] Seq|n  Num|k  then per order j<k:
#                      Cmd<j>| Arg<j>| Ship<j>| Text<j>=
#                      Ship = fleet slot 0..MAX_FLEET-1, or -1 = broadcast.
#
# The config mapping has NO delete and LoadConfigFile MERGES (verified
# 2026-08-04), so all telemetry uses FIXED slot keys (S0..S11, F0..F7) and
# readers gate every block on its St key.  Stale values from earlier rounds
# linger in the file; St tells you what is real.
#
# Order codes (Cmd / Arg, Text where noted):
#   1 STANCE   1 aggressive  2 balanced  3 defensive  4 ambush  5 cripple
#   2 TARGET   subsystem of that ship's CURRENT TARGET: 0 none 1 hull
#              2 shields 3 weapons 4 torps 5 engines 6 warp 7 sensors
#              8 power 9 cloak
#   3 POWER    1 weapons  2 shields  3 engines  4 balanced
#   4 CLOAK    1 on  0 off
#   5 TAUNT    Text; addressed = that ship speaks, broadcast = Claude speaks
#   6 END      stand the whole link down (ships keep fighting on last AI)
#   7 BREAK    Arg = seconds to disengage (0 cancel), clamp 3..30
#   8 CIRCLE   Arg = seconds orbiting guns-out (0 cancel), clamp 5..60
#   9 GUARD    same but presenting the strongest shield
#  10 SPAWN    Arg = side (0 enemy / 1 friendly), Text = "ShipClass" or
#              "ShipClass,Display Name".  Ship field ignored.
#  11 FOCUS    Arg = foe slot 0..7, 8 = the player, 99 = release.
#              Re-asserted every tick via SetTarget (NOTE: steering the
#              stock attack AI's target choice this way is UNPROVEN against
#              SelectTarget's 5 s re-pick; verify live, fall back to AI
#              rebuild with a narrowed group if it does not stick).
#  12 FREE     hand the ship to the stock QuickBattle AI (side-appropriate)
#              and stop micromanaging it; any later STANCE order re-adopts.
#  18 WARP     warp break: in-system warp WARP_DIST units away from the
#              current target, mend (shields power + repair bay) for Arg
#              seconds (0 cancel, clamp 10..90), then hand back to the attack
#              AI - whose own Intercept warps back in from that distance.
#
# v2 provenance kept: open() is dead (proved 2026-08-03 four ways), the
# engine resolves bare cfg filenames against the game root, [ClaudeOrders]
# leaks into Options.cfg harmlessly, manoeuvres are TIME BOXED IN PYTHON
# (timer event + tick watchdog, either alone suffices), and the ship always
# has a normal attack AI between orders so Claude latency never stalls it.
#
# Save safety: module globals hold only ints, floats, strings, and
# lists/dicts of the same (ships tracked BY NAME, never by pointer).
# Engine objects grabbed at import time are in NonSerializedObjects.
##############################################################################

import App
import string
import math
import sys


def DebugPrint(sText):
	# sys.stdout.write keeps this file parseable by both Python 1.5.2 (the
	# game) and Python 3 (offline syntax checks) - LiveVoice convention.
	sys.stdout.write("ClaudeCommander: " + sText + "\n")


def SafeErr():
	try:
		return str(sys.exc_type) + ": " + str(sys.exc_value)
	except:
		return "unknown error"


NonSerializedObjects = ( "pGame", )

App.ET_INPUT_CLAUDE_DUEL = App.UtopiaModule_GetNextEventType()
App.ET_CLAUDE_TICK = App.UtopiaModule_GetNextEventType()
App.ET_CLAUDE_MANEUVER_END = App.UtopiaModule_GetNextEventType()
App.ET_INPUT_BORG_DRIVE = App.UtopiaModule_GetNextEventType()

TICK_TIME = 1.0
NO_PLAYER_GRACE = 3		# ticks with no player before the link stands down;
				# a TransferCommand swap legitimately has none
SPAWN_RANGE = 5000.0		# enemy-side spawn: metres ahead of the player

# Live-tunable engagement distance (order 17).  RED spawns SPAWN_RANGE ahead of
# the player and BLUE spawns beside them, so this value IS the distance the two
# sides start apart.  At the 5000 default a DS9Wings (speed 8.4, beams 250,
# pulses 500) can never close inside a round - 4000m takes ~900s - so a mirror
# match is decided entirely by its 8 torpedo tubes while 60 emitters sit idle.
# Dropping it is the only way to make short-ranged or slow hulls fight properly.
g_fSpawnRange = SPAWN_RANGE
FRIEND_JITTER = 600		# friendly-side spawn: DistressCall-style jitter

MAX_FLEET = 24
MAX_FOES = 8

STATE_CFG = "claude_state.cfg"
ORDER_CFG = "claude_orders.cfg"
SEC_STATE = "ClaudeState"
SEC_ORDERS = "ClaudeOrders"

# QuickBattle's guard name for AIs built against an empty target group
# (FedAttack.py:11 does GetNameTuple()[0] and dies otherwise).
PHANTOM_NAME = "This ship probably wont exist"

# Status codes for the state file
ST_IDLE = 1
ST_ACTIVE = 2
ST_PLAYER_DEAD = 4
ST_OVER = 5

# Slot status codes
SLOT_EMPTY = 0
SLOT_ALIVE = 1
SLOT_DEAD = 2

# Focus / target codes
FOCUS_PLAYER = 8
FOCUS_SPAR = 9		# sparring: fight the other side but NEVER the human player
FOCUS_NONE = 99
SPAR_TGT_BASE = 100		# telemetry Tgt of a spar-focused ship: 100 + the opposing FLEET slot

##############################################################################
# AUTOBREAK (order 13) - game-side defensive reflex.
#
# Claude polls this bridge every 10-15s; the game runs at 1 Hz.  Rounds 6 and 7
# (2026-08-05) were both lost inside a single polling gap - round 7's killing
# blow took the ship 76% -> destroyed between two samples, and the BREAK order
# Claude sent came back "S1 not alive".  No amount of tactical thinking helps
# when the whole decisive event falls between two reads.
#
# So the reflex moves in-engine, where it can fire on the tick it is needed,
# and Claude keeps only the slow decisions (ship choice, focus, when to
# commit).  Same split as the rest of the design: the AI keeps fighting
# between orders so Claude's latency never stalls the battle.
##############################################################################

AB_DEF_HULL = 40		# break at or below this hull %
AB_DEF_FACE = 20		# ...or when any live shield facing drops this low
AB_DEF_DROP = 12		# ...or on losing this much hull inside AB_WINDOW
AB_DEF_SECS = 12		# break duration
AB_WINDOW = 5			# ticks (= seconds) the drop test looks back over
AB_COOLDOWN = 25		# ticks after a break before it may fire again

lStanceNames = ( "", "aggressive", "balanced", "defensive", "ambush",
		 "cripple", "gunboat", "free" )
lTargetNames = ( "none", "hull", "shields", "weapons", "torps", "engines",
		 "warp", "sensors", "power", "cloak" )
lPowerNames = ( "", "weapons", "shields", "engines", "balanced" )

g_bActive = 0
g_iOrders = 0			# last order batch Seq consumed
g_iTick = 0
g_idButton = App.NULL_ID
g_iNoPlayer = 0			# consecutive ticks with no player
g_idDriveButton = App.NULL_ID
g_bBorgDrive = 0		# player-ship collision override (the player's Cube)
g_bSaveErrShown = 0
g_iSerial = 1			# display-name serial (GetFreeName)
g_iManGen = 0			# maneuver generation counter, shared by all slots
g_sLastResult = ""		# last batch's order results, for telemetry

# The fleet: MAX_FLEET fixed slots, each a dict of ints/floats/strings only.
# Slots are never reused within a battle; dead ships keep their slot with
# st = SLOT_DEAD so telemetry stays stable for the outside reader.
g_lFleet = []
# The foes: every non-fleet, non-player ship seen in the set, first come
# first slotted, slot kept after death.
g_lFoes = []


def NewFleetSlot():
	return { "st": SLOT_EMPTY, "name": "", "type": "", "side": 0,
		 "stance": "balanced", "tsub": "", "focus": FOCUS_NONE,
		 "man": "", "manGen": 0, "manEnd": 0, "manStart": 0,
		 "manRange0": -1, "manFail": 0, "manNoAvoid": 0, "needRestore": 0,
		 "noAIWarn": 0, "lastRange": -1.0, "lastTgt": FOCUS_NONE,
		 "aiTgt": "", "written": 0, "closeRange": 0.0,
		 "abOn": 0, "abHull": AB_DEF_HULL, "abFace": AB_DEF_FACE,
		 "abDrop": AB_DEF_DROP, "abSecs": AB_DEF_SECS,
		 "abNext": 0, "abFires": 0, "abHist": [],
		 "wbPhase": 0, "wbOutEnd": 0, "wbMendEnd": 0, "wbMend": 0,
		 "wbWarped": 0, "wbWasWarping": 0, "wbStarts": 0, "spar": 0 }


def NewFoeSlot():
	return { "st": SLOT_EMPTY, "name": "", "type": "", "side": 0,
		 "written": 0 }


def ResetSlots():
	global g_lFleet, g_lFoes
	g_lFleet = []
	g_lFoes = []
	i = 0
	while i < MAX_FLEET:
		g_lFleet.append(NewFleetSlot())
		i = i + 1
	i = 0
	while i < MAX_FOES:
		g_lFoes.append(NewFoeSlot())
		i = i + 1


ResetSlots()


def InQuickBattle():
	pCurGame = App.Game_GetCurrentGame()
	if pCurGame == None:
		return 0
	try:
		if (pCurGame.GetScript() == "Maelstrom.Maelstrom"):
			return 0
	except:
		pass
	return 1


def GetHelmMenu():
	# Borg Drive lives on Helm (the player, 2026-08-14) - it is a flight/collision
	# behaviour, not a weapon.  Appended, so it sits at the bottom.
	pTactCtrlWindow = App.TacticalControlWindow_GetTacticalControlWindow()
	if pTactCtrlWindow == None:
		return None
	pDatabase = App.g_kLocalizationManager.Load("data/TGL/Bridge Menus.tgl")
	if pDatabase is None:
		return None
	return pTactCtrlWindow.FindMenu(pDatabase.GetString("Helm"))


def GetTacticalMenu():
	pTactCtrlWindow = App.TacticalControlWindow_GetTacticalControlWindow()
	if pTactCtrlWindow == None:
		return None
	pDatabase = App.g_kLocalizationManager.Load("data/TGL/Bridge Menus.tgl")
	if pDatabase is None:
		return None
	return pTactCtrlWindow.FindMenu(pDatabase.GetString("Tactical"))


def Announce(sText):
	try:
		pSequence = App.TGSequence_Create()
		pSubtitleAction = App.SubtitleAction_CreateC(sText)
		pSubtitleAction.SetDuration(6.0)
		pSequence.AddAction(pSubtitleAction)
		pSequence.Play()
	except:
		DebugPrint(sText)
	try:
		App.STMissionLog_GetMissionLog().AddLine(App.TGString(sText))
	except:
		pass


def GetShipByName(sName):
	if sName == "":
		return None
	import MissionLib
	pShip = MissionLib.GetShip(sName)
	if pShip == None:
		return None
	return App.ShipClass_Cast(pShip)


def FleetShip(iSlot):
	if (iSlot < 0) or (iSlot >= MAX_FLEET):
		return None
	if g_lFleet[iSlot]["st"] != SLOT_ALIVE:
		return None
	return GetShipByName(g_lFleet[iSlot]["name"])


def FoeShip(iSlot):
	if (iSlot < 0) or (iSlot >= MAX_FOES):
		return None
	if g_lFoes[iSlot]["st"] != SLOT_ALIVE:
		return None
	return GetShipByName(g_lFoes[iSlot]["name"])


def OwnGroup(iSide):
	# The group a ship of this side BELONGS to.  MissionLib handles stay
	# live across QuickBattle Terminate/Initialize, unlike the module
	# globals (which go None) - verified 2026-08-04.
	import MissionLib
	if iSide == 1:
		return MissionLib.GetFriendlyGroup()
	return MissionLib.GetEnemyGroup()


def TargetGroup(iSide):
	# The group a ship of this side SHOOTS at.
	import MissionLib
	if iSide == 1:
		return MissionLib.GetEnemyGroup()
	return MissionLib.GetFriendlyGroup()


def PlayerName():
	pPlayer = App.Game_GetCurrentPlayer()
	if pPlayer == None:
		return ""
	try:
		return pPlayer.GetName()
	except:
		return ""


##############################################################################
# AI construction (side-aware v3 of the v2 stance system)
##############################################################################

dStances = {
	# name: (difficulty, extra flag overrides)
	"aggressive":	(1.0, {"UseCloaking": 0, "NeverSitStill": 1, "AvoidTorps": 0,
			       "WarpOutBeforeDying": 0}),
	"balanced":	(0.7, {"UseCloaking": 0, "AvoidTorps": 1}),
	"defensive":	(0.7, {"UseCloaking": 1, "AvoidTorps": 1, "SmartShields": 1,
			       "NeverSitStill": 1, "WarpOutBeforeDying": 0}),
	"ambush":	(1.0, {"UseCloaking": 1, "AvoidTorps": 1, "SmartPhasers": 1}),
	"cripple":	(1.0, {"UseCloaking": 0, "DisableOnly": 1, "DisableBeforeDestroy": 1,
			       "ChooseSubsystemTargets": 1}),
	# Short-reach NonFedAttack hulls (2026-09-10).  Stock NonFedAttack puts
	# its close band at 120-150, outside a DefiantNX and its 100-unit cannons,
	# and at the 0.7 band NeverSitStill hands that band to TorpedoRun, which
	# crawls whenever the target is off the nose: two such hulls lock into a
	# point-blank circle with each other on the beam (787 ticks, not one hit).
	# gunboat builds AI.Compound.GunboatAttack (see GunboatAttackAI) - the same
	# tree with the close band inside the guns - with NeverSitStill 0, so that
	# band stops and pivots (GunboatHold) instead of circling.
	"gunboat":	(0.7, {"UseCloaking": 0, "AvoidTorps": 1, "NeverSitStill": 0}),
}


# Subsystem keyword -> engine class-type constant, for the TargetSubsystems
# keyword the attack AI's FireScript actually reads (FedAttack.py:944 forwards
# every keyword).  ShipClass.SetTargetSubsystem alone only moves the HUD
# pointer - proved in round 2.
dTargetClasses = {
	"hull":		"CT_HULL_SUBSYSTEM",
	"shields":	"CT_SHIELD_SUBSYSTEM",
	"weapons":	"CT_WEAPON_SYSTEM",
	"torps":	"CT_TORPEDO_SYSTEM",
	"engines":	"CT_IMPULSE_ENGINE_SUBSYSTEM",
	"warp":		"CT_WARP_ENGINE_SUBSYSTEM",
	"sensors":	"CT_SENSOR_SUBSYSTEM",
	"power":	"CT_POWER_SUBSYSTEM",
	"cloak":	"CT_CLOAKING_SUBSYSTEM",
}


def TargetClass(sKeyword):
	if not dTargetClasses.has_key(sKeyword):
		return None
	try:
		return getattr(App, dTargetClasses[sKeyword])
	except:
		return None


def GuardTargetGroup(pTargets):
	# FedAttack.py:11 does GetNameTuple()[0]; an empty group means no AI.
	# QuickBattle itself plants a phantom name (QuickBattle.py:3811-3812);
	# do the same.
	try:
		if not pTargets.GetNameTuple():
			pTargets.AddName(PHANTOM_NAME)
	except:
		pass


def IsSparShip(sName):
	# Asked by Surrender.py and AbandonShip.py before an AI crew may quit.  A
	# DUELAI spar round measures one side against the other, and a hull that
	# surrenders or bails out ends the experiment instead of the fight (the player,
	# 2026-09-10).  Spar ships are exactly the ones under spar focus - duel
	# ships never are.  len(): the fleet table is empty until a link starts.
	if sName == "":
		return 0
	i = 0
	while i < len(g_lFleet):
		dSlot = g_lFleet[i]
		# "spar" sticks once a ship has been spar-focused: a brains commander
		# re-focusing it on one enemy (focus 100+N) must not quietly hand it
		# back to Surrender/AbandonShip/PointDefence (2026-10-24).
		if (dSlot["st"] == SLOT_ALIVE) and (dSlot["name"] == sName) and \
		   ((dSlot["focus"] == FOCUS_SPAR) or dSlot["spar"]):
			return 1
		i = i + 1
	return 0


def SparNames(iSlot):
	# Everyone on the opposing side EXCEPT the human player, sorted: what
	# the spar target group holds, and (as SparRoster) its identity.
	dSlot = g_lFleet[iSlot]
	pGroup = TargetGroup(dSlot["side"])
	if pGroup == None:
		return []
	sPlayer = PlayerName()
	lNames = []
	try:
		tNames = pGroup.GetNameTuple()
	except:
		return []
	for sName in tNames:
		if sName != sPlayer:
			lNames.append(sName)
	lNames.sort()
	return lNames


def SparRoster(iSlot):
	# What a spar-focused AI was built against.  FocusGroup records it as
	# aiTgt and FocusName returns it, so ValidateFocus rebuilds the AI when
	# the opposing roster changes - and only then.
	return "spar:" + string.join(SparNames(iSlot), "|")


def SparGroup(iSlot):
	# Sparring target group: everyone on the opposing side EXCEPT the
	# human player.  Same lever as FocusGroup - the AI picks only from
	# the group it is handed - so the player becomes invisible to this
	# ship without touching the mission groups themselves.
	lNames = SparNames(iSlot)
	if len(lNames) == 0:
		return None
	try:
		return App.ObjectGroup_ForceToGroup(lNames)
	except:
		return None


def FocusGroup(iSlot):
	# The group the attack AI is allowed to pick from, when a FOCUS order is
	# in force.  Returns None to mean "no focus - use the whole side group".
	#
	# Why this exists: v3 implemented FOCUS as a per-tick ShipClass.SetTarget
	# in AssertTargets, and that never worked.  SelectTarget chooses solely
	# from its own target group (Preprocessors.py:1432) and then OVERWRITES
	# the ship's target with its own pick every 5s (Preprocessors.py:1259-63,
	# fNormalUpdateTime=5.0).  So our pointer survived at most one poll - the
	# order looked acked in telemetry while the AI did as it pleased.  It was
	# flagged UNPROVEN in the design notes; 2026-08-05 proved it broken.
	#
	# Handing the AI a one-ship group is the only lever that actually steers
	# it: with a single candidate FindGoodTarget short-circuits and returns
	# that ship with no scoring at all (Preprocessors.py:1462-1464).
	#
	# The group is rebuilt (not mutated) on every AI build, so nothing but
	# strings is ever held across a save.
	dSlot = g_lFleet[iSlot]
	if dSlot["focus"] == FOCUS_SPAR:
		# The roster, not a fixed label.  The old "spar (not the player)"
		# never equalled FocusName (which resolved the PLAYER for a red
		# ship), so ValidateFocus rebuilt every spar-focused AI every tick
		# and AssertTargets kept pointing it back at the player.
		dSlot["aiTgt"] = SparRoster(iSlot)
		return SparGroup(iSlot)
	sName = FocusName(iSlot)
	dSlot["aiTgt"] = sName
	if sName == "":
		return None
	try:
		return App.ObjectGroup_ForceToGroup( [ sName ] )
	except:
		dSlot["aiTgt"] = ""
		return None


def FocusName(iSlot):
	# Who this ship's attack AI should be locked to, "" for "no focus".
	dSlot = g_lFleet[iSlot]
	if dSlot["focus"] == FOCUS_NONE:
		return ""
	if dSlot["focus"] == FOCUS_SPAR:
		return SparRoster(iSlot)
	iCode, pTarget = ResolveTarget(iSlot)
	if pTarget == None:
		return ""
	try:
		return pTarget.GetName()
	except:
		return ""


def ValidateFocus():
	# The focus lives in the AI's target group, which is fixed at build
	# time, so any drift between "who we should be locked to" and "who we
	# were actually built against" needs a rebuild.  Self-healing by
	# construction: covers the focused foe dying, its foe slot being
	# recycled under us, and a focus set while a manoeuvre was running.
	i = 0
	while i < MAX_FLEET:
		dSlot = g_lFleet[i]
		if ((dSlot["st"] == SLOT_ALIVE) and (dSlot["stance"] != "free")
		    and (dSlot["man"] == "")):
			iFocus = dSlot["focus"]
			if ((iFocus >= 0) and (iFocus < MAX_FOES)
			    and (g_lFoes[iFocus]["st"] != SLOT_ALIVE)):
				dSlot["focus"] = FOCUS_NONE
				DebugPrint("[focus] S" + str(i) +
					   " target gone - focus released")
			if ((iFocus >= SPAR_TGT_BASE) and (iFocus < SPAR_TGT_BASE + MAX_FLEET)
			    and (g_lFleet[iFocus - SPAR_TGT_BASE]["st"] != SLOT_ALIVE)):
				dSlot["focus"] = FOCUS_NONE
				DebugPrint("[focus] S" + str(i) +
					   " fleet target gone - focus released")
			if FocusName(i) != dSlot["aiTgt"]:
				RefocusAI(i)
		i = i + 1


def RefocusAI(iSlot):
	# Focus only takes effect through the AI's target group, so a focus
	# change means an AI rebuild.  Mid-manoeuvre we skip it: EndManeuver
	# rebuilds through ApplyStance anyway and will pick the new focus up,
	# and rebuilding here would silently cancel the manoeuvre.
	dSlot = g_lFleet[iSlot]
	if dSlot["st"] != SLOT_ALIVE:
		return 0
	if dSlot["stance"] == "free":
		return 0
	if dSlot["man"] != "":
		return 0
	return ApplyStance(iSlot, dSlot["stance"])


def GunboatAttackAI(pShip, pTargets, dFlags, dSlot):
	# The gunboat stance attack AI.  BasicAttack would take the module from
	# the hull AIString (DefiantNX: NonFedAttack, .pyc-only), so do its two
	# jobs here - expand the difficulty flags, then build - and hand over the
	# close band as a keyword.  closeRange arrives in order 1 text.
	import AI.Compound.BasicAttack
	import AI.Compound.GunboatAttack
	pShip = App.ShipClass_Cast(pShip)
	sPrefix = ("Easy_", "", "Hard_")[App.Game_GetDifficulty()]
	AI.Compound.BasicAttack.SetFlagsFromDifficulty(sPrefix, dFlags)
	if dSlot.get("closeRange", 0.0) > 0.0:
		dFlags["CloseRange"] = dSlot["closeRange"]
	return apply(AI.Compound.GunboatAttack.CreateAI, (pShip, pTargets), dFlags)


def BuildAI(pShip, iSlot, sStance, bChooseSubsystems):
	import AI.Compound.BasicAttack

	dSlot = g_lFleet[iSlot]
	fDifficulty, dExtra = dStances.get(sStance, dStances["balanced"])

	dFlags = {}
	for sKey in dExtra.keys():
		dFlags[sKey] = dExtra[sKey]
	dFlags["Difficulty"] = fDifficulty
	dFlags["FollowTargetThroughWarp"] = 1
	if not bChooseSubsystems:
		# We are picking the subsystem by hand; stop the AI fighting us
		# over it, and hand the choice to the FireScript that aims.
		dFlags["ChooseSubsystemTargets"] = 0
		eClass = TargetClass(dSlot["tsub"])
		if eClass != None:
			dFlags["TargetSubsystems"] = [ (eClass, 1) ]

	pTargets = FocusGroup(iSlot)
	if (pTargets == None) and (dSlot["focus"] == FOCUS_SPAR):
		# A spar ship with nobody across from it yet gets an empty group,
		# never the mission group - for red, that one holds the player.
		pTargets = App.ObjectGroup_ForceToGroup( [ PHANTOM_NAME ] )
	if pTargets == None:
		pTargets = TargetGroup(dSlot["side"])
	GuardTargetGroup(pTargets)
	pAttack = None
	if sStance == "gunboat":
		try:
			pAttack = GunboatAttackAI(pShip, pTargets, dFlags, dSlot)
		except:
			DebugPrint("S" + str(iSlot) + " gunboat AI failed (" + SafeErr() + ") - stock attack AI")
			pAttack = None
	if pAttack == None:
		pAttack = AI.Compound.BasicAttack.CreateAI(pShip, pTargets, Keywords = dFlags)

	# Fallback wander, so the ship never just sits there if it loses its
	# target (straight out of QuickBattleAI).
	pTurn = App.PlainAI_Create(pShip, "Turn")
	pTurn.SetScriptModule("ManeuverLoop")
	pTurn.SetInterruptable(1)
	pScript = pTurn.GetScriptInstance()
	pScript.SetLoopFraction(0.25)
	pScript.SetTurnAxis(App.TGPoint3_GetModelLeft())

	pTurn_2 = App.PlainAI_Create(pShip, "Turn_2")
	pTurn_2.SetScriptModule("ManeuverLoop")
	pTurn_2.SetInterruptable(1)
	pScript = pTurn_2.GetScriptInstance()
	pScript.SetLoopFraction(0.25)
	pScript.SetTurnAxis(App.TGPoint3_GetModelUp())

	pFly = App.RandomAI_Create(pShip, "FlyPointlessly")
	pFly.SetInterruptable(1)
	pFly.AddAI(pTurn)
	pFly.AddAI(pTurn_2)

	pRepeatForever = App.SequenceAI_Create(pShip, "RepeatForever")
	pRepeatForever.SetInterruptable(1)
	pRepeatForever.SetLoopCount(-1)
	pRepeatForever.SetResetIfInterrupted(1)
	pRepeatForever.SetDoubleCheckAllDone(1)
	pRepeatForever.SetSkipDormant(0)
	pRepeatForever.AddAI(pFly)

	pPriorityList = App.PriorityListAI_Create(pShip, "PriorityList")
	pPriorityList.SetInterruptable(1)
	pPriorityList.AddAI(pAttack, 1)
	pPriorityList.AddAI(pRepeatForever, 2)

	import AI.Preprocessors
	pAvoid = App.PreprocessingAI_Create(pShip, "AvoidObstacles")
	pAvoid.SetInterruptable(1)
	pAvoid.SetPreprocessingMethod(TunedAvoid(), "Update")
	pAvoid.SetContainedAI(pPriorityList)

	return pAvoid


# Shared avoider tuning.  See ManeuverAvoid for the full root-cause writeup;
# the short version is that stock AvoidObstacles treats ANYTHING inside
# ship_radius * fPersonalSpace as a hazard without ever consulting
# fPredictionTime (Preprocessors.py:1778-1781), floors its scan radius at
# fMinimumRadius=225 (:1644), and when it overrides it suppresses the whole
# contained AI and commands SetImpulse(0.0) unless the nose already points
# somewhere safe (:1917-1923).
AVOID_PERSONAL_SPACE = 1.1	# stock 2.5 - the number that caused the standoff
AVOID_MIN_RADIUS = 60.0		# stock 225.0
AVOID_ATTACK_PREDICT = 2.5	# stock 15.0
AVOID_MANEUVER_PREDICT = 1.5


def TuneAvoider(pScript, fPredict):
	try:
		pScript.fPredictionTime = fPredict
		pScript.fPersonalSpace = AVOID_PERSONAL_SPACE
		pScript.fMinimumRadius = AVOID_MIN_RADIUS
	except:
		pass
	return pScript


PATCH_STOCK_AVOIDER = 1		# set 0 to leave stock AI ships alone


def PatchStockAvoider():
	# Ships that Claude does NOT command build their own AvoidObstacles with
	# the stock fPersonalSpace=2.5 and stall in exactly the same way - that
	# is every Call Wave arrival, every QuickBattle menu ship, Distress Call
	# and Battle Mode.  Tuning only Claude's own avoiders would leave the player's
	# ships parked at 300m, so patch the class itself and every future
	# instance inherits the fix.
	#
	# 1.5.2 notes: there are no closures, so the original __init__ has to
	# ride in on a default argument; and assigning a plain function to an
	# old-style class turns it into an unbound method that receives self.
	if not PATCH_STOCK_AVOIDER:
		return
	try:
		import AI.Preprocessors
		pClass = AI.Preprocessors.AvoidObstacles
		if hasattr(pClass, "claudeTuned"):
			return

		fOldInit = pClass.__init__

		def TunedInit(self, fOldInit = fOldInit):
			fOldInit(self)
			try:
				self.fPersonalSpace = AVOID_PERSONAL_SPACE
				self.fMinimumRadius = AVOID_MIN_RADIUS
				if self.fPredictionTime > AVOID_ATTACK_PREDICT:
					self.fPredictionTime = AVOID_ATTACK_PREDICT
			except:
				pass

		pClass.__init__ = TunedInit
		pClass.claudeTuned = 1
		DebugPrint("stock AvoidObstacles tuned - personal space " +
			   str(AVOID_PERSONAL_SPACE) + " (was 2.5), min radius " +
			   str(AVOID_MIN_RADIUS) + " (was 225)")
	except:
		DebugPrint("could not tune stock avoider: " + SafeErr())


def TunedAvoid():
	# The ATTACK AI's avoider.
	#
	# 2026-08-04 this only pulled fPredictionTime in from 15.0 to 4.0, which
	# fixed the nose-on stall between two ships but did NOTHING for a fleet:
	# personal space is checked before prediction time is ever consulted, so
	# with 10+ hulls in one place CLAUDE'S OWN SHIPS BECAME EACH OTHER'S
	# OBSTACLES.  Observed live 2026-08-05: twelve ships parked at 300-400m
	# with closing rates of -1 to -3 while 45 torpedoes were in the air, the
	# target's hull sat unchanged at 52% and its battery CLIMBED from 76 to
	# 96.  Ordering orbits was only ever a workaround for this.
	import AI.Preprocessors
	return TuneAvoider(AI.Preprocessors.AvoidObstacles(), AVOID_ATTACK_PREDICT)


def ApplyStance(iSlot, sStance):
	pShip = FleetShip(iSlot)
	if pShip == None:
		return 0
	if not dStances.has_key(sStance):
		return 0
	dSlot = g_lFleet[iSlot]
	dSlot["stance"] = sStance
	bChoose = 1
	if dSlot["tsub"] != "":
		bChoose = 0
	try:
		pShip.SetAI(BuildAI(pShip, iSlot, sStance, bChoose), 0, 0)
	except:
		# Retry once letting the AI pick subsystems itself: NonFedAttack
		# is .pyc and may not forward TargetSubsystems on every hull.
		DebugPrint("AI build failed (" + SafeErr() + ") - retrying without subsystem list")
		try:
			pShip.SetAI(BuildAI(pShip, iSlot, sStance, 1), 0, 0)
		except:
			DebugPrint("could not rebuild AI for slot " + str(iSlot) + ": " + SafeErr())
			return 0
	# Installing the attack AI IS the end of a manoeuvre.
	dSlot["man"] = ""
	dSlot["manEnd"] = 0
	dSlot["manStart"] = 0
	dSlot["manRange0"] = -1
	dSlot["needRestore"] = 0
	dSlot["manFail"] = 0
	dSlot["manNoAvoid"] = 0
	dSlot["wbPhase"] = 0
	return 1


def ApplyStockAI(iSlot):
	# FREE order: hand the ship to the stock side-appropriate QuickBattle
	# AI.  g_iCurrentAILevel is -1 outside StartSimulation2's loop, which
	# BasicAttack clamps to the dumbest AI - set it first.
	pShip = FleetShip(iSlot)
	if pShip == None:
		return 0
	dSlot = g_lFleet[iSlot]
	try:
		import QuickBattle.QuickBattle
		QuickBattle.QuickBattle.g_iCurrentAILevel = 1.0
	except:
		pass
	GuardTargetGroup(TargetGroup(dSlot["side"]))
	if dSlot["side"] == 1:
		sModule = "QuickBattle.QuickBattleFriendlyAI"
	else:
		sModule = "QuickBattle.QuickBattleAI"
	try:
		# __import__("A.B") returns package A, not module B (stock code
		# only gets away with the bare form because it imports from
		# INSIDE the QuickBattle package).  The fromlist form returns
		# the leaf; sys.modules is the belt-and-braces fallback.
		pAIModule = __import__(sModule, {}, {}, ["CreateAI"])
		if not hasattr(pAIModule, "CreateAI"):
			pAIModule = sys.modules[sModule]
		pShip.SetAI(pAIModule.CreateAI(pShip), 0, 0)
	except:
		DebugPrint("stock AI failed for slot " + str(iSlot) + ": " + SafeErr())
		return 0
	dSlot["stance"] = "free"
	dSlot["tsub"] = ""
	dSlot["focus"] = FOCUS_NONE
	dSlot["man"] = ""
	dSlot["manEnd"] = 0
	dSlot["needRestore"] = 0
	dSlot["manFail"] = 0
	return 1


##############################################################################
# Manoeuvre AIs  (orders 7 BREAK / 8 CIRCLE / 9 GUARD) - per-slot v3.
# Same verified APIs as v2; every manoeuvre is time boxed from Python by a
# TGIntEvent timer AND the tick watchdog, either alone restores the attack AI.
##############################################################################

MAN_BREAK = 7
MAN_CIRCLE = 8			# orbit guns-out (shield importance 0.0)
MAN_GUARD = 9			# orbit strongest-shield-out (weapon importance 0.0)

BREAK_DEF_SEC = 8
BREAK_MIN_SEC = 3
BREAK_MAX_SEC = 30

CIRCLE_DEF_SEC = 20
CIRCLE_MIN_SEC = 5
CIRCLE_MAX_SEC = 60

CIRCLE_NEAR = 150.0
CIRCLE_FAR = 220.0
CIRCLE_SPEED = 0.8
CIRCLE_OPEN_BIAS = -0.35
CIRCLE_USE_FIXED = 0

MAN_GRACE_TICKS = 4
BREAK_WANT_OPEN = 40
BREAK_HARD_CLOSE = 200


def MakeGoForward(pShip, fImpulse):
	# SetImpulse has no default and IS required - omitting it makes
	# BaseAI.Activate raise at the first update, outside our try/except.
	pGo = App.PlainAI_Create(pShip, "ClaudeRun")
	pGo.SetScriptModule("GoForward")
	pGo.SetInterruptable(1)
	pScript = pGo.GetScriptInstance()
	pScript.SetImpulse(fImpulse)
	return pGo


def MakeFlee(pShip, sTargetName):
	# Shape of FedAttack.py:860-864.  Flee never returns DONE while the
	# target lives; the timer and watchdog own the ending.
	pFlee = App.PlainAI_Create(pShip, "ClaudeBreak")
	pFlee.SetScriptModule("Flee")
	pFlee.SetInterruptable(1)
	pScript = pFlee.GetScriptInstance()
	pScript.SetFleeFromGroup(sTargetName)
	return pFlee


def MakeCircle(pShip, sTargetName, fBias, fShieldImp, fWeaponImp):
	pICO = App.PlainAI_Create(pShip, "ClaudeCircle")
	pICO.SetScriptModule("IntelligentCircleObject")
	pICO.SetInterruptable(1)
	pScript = pICO.GetScriptInstance()
	pScript.SetFollowObjectName(sTargetName)
	# MANDATORY: BaseAI.SetupDefaultParams calls this with no arguments,
	# which leaves bUseRoughDistances=0 and pins the ship in place.
	pScript.SetRoughDistances(CIRCLE_NEAR, CIRCLE_FAR)
	pScript.SetCircleSpeed(CIRCLE_SPEED)
	# AdjustFacing is an if/elif chain, NOT a weighted blend - whichever
	# importance is non-zero wins outright.
	pScript.SetShieldAndWeaponImportance(fShieldImp, fWeaponImp)
	pScript.SetForwardBias(fBias)
	if CIRCLE_USE_FIXED:
		try:
			pScript.UseFixedCode(1)
		except:
			pass
	return pICO


def WrapFire(pShip, pInner, sTargetName, sTSub):
	# CIRCLE/GUARD keep the guns hot.  Mirrors FedAttack.py:938-952.
	import AI.Preprocessors
	dKw = {}
	if sTSub == "":
		dKw["ChooseSubsystemTargets"] = 1
	pFireScript = apply(AI.Preprocessors.FireScript, (sTargetName,), dKw)
	lSystems = [ pShip.GetTorpedoSystem(), pShip.GetPhaserSystem(),
		     pShip.GetPulseWeaponSystem() ]
	for pSystem in lSystems:
		if not App.IsNull(pSystem):
			pFireScript.AddWeaponSystem(pSystem)
	pFire = App.PreprocessingAI_Create(pShip, "ClaudeManFire")
	pFire.SetInterruptable(1)
	pFire.SetPreprocessingMethod(pFireScript, "Update")
	pFire.SetContainedAI(pInner)
	return pFire


def ManeuverAvoid():
	# A much tighter avoider than the attack AI's.
	#
	# ROOT CAUSE of round 8's useless BREAKs (2026-08-05, read from source):
	# AvoidObstacles.NeedToAvoid returns 1 for ANYTHING already inside
	# personal space (ship radius * fPersonalSpace) without ever consulting
	# fPredictionTime (Preprocessors.py:1778-1781) - so the earlier
	# fPredictionTime tuning did nothing at knife range - and the scan
	# radius has a 225-unit floor (fMinimumRadius, :1644).  Worse, when it
	# does override it returns PS_SKIP_ACTIVE (contained AI fully
	# suppressed) and picks fSpeed = 0.0 unless the nose ALREADY points
	# somewhere safe (:1917-1923).  In a brawl with a carrier and fighters
	# inside 200m that fires every tick, so the ship parks and rotates
	# instead of fleeing - range never opened, and Fable was shot apart
	# while nominally "breaking off" twice.
	#
	# Pulling personal space in to ~1.1x radius means only a genuinely
	# imminent collision overrides the manoeuvre; the watchdog escalation
	# below handles whatever still slips through.
	import AI.Preprocessors
	return TuneAvoider(AI.Preprocessors.AvoidObstacles(), AVOID_MANEUVER_PREDICT)


def WrapAvoid(pShip, pInner):
	pAvoid = App.PreprocessingAI_Create(pShip, "ClaudeManAvoid")
	pAvoid.SetInterruptable(1)
	pAvoid.SetPreprocessingMethod(ManeuverAvoid(), "Update")
	pAvoid.SetContainedAI(pInner)
	return pAvoid


def BuildManeuverAI(pShip, iSlot, iKind, sTargetName, iRangeNow, bNoAvoid = 0):
	# Returns an AI object or None.  NEVER installs it.
	# bNoAvoid drops the AvoidObstacles wrapper entirely - the escalation
	# the watchdog reaches for when a BREAK is being parked by the avoider.
	# Flying into a rock is survivable; being held at knife range is not.
	pLeaf = None
	pBackup = None

	try:
		pBackup = MakeGoForward(pShip, 1.0)
	except:
		pBackup = None
		DebugPrint("GoForward backup unavailable: " + SafeErr())

	if iKind == MAN_BREAK:
		try:
			pLeaf = MakeFlee(pShip, sTargetName)
		except:
			pLeaf = None
			DebugPrint("Flee build FAILED: " + SafeErr())
	else:
		fBias = 0.0
		if (iRangeNow >= 0) and (iRangeNow < int(CIRCLE_NEAR)):
			fBias = CIRCLE_OPEN_BIAS
		if iKind == MAN_GUARD:
			fShieldImp, fWeaponImp = 1.0, 0.0
		else:
			fShieldImp, fWeaponImp = 0.0, 1.0
		try:
			pLeaf = MakeCircle(pShip, sTargetName, fBias, fShieldImp, fWeaponImp)
		except:
			pLeaf = None
			DebugPrint("IntelligentCircleObject build FAILED: " + SafeErr())

	if (pLeaf == None) and (pBackup == None):
		return None

	if (pLeaf != None) and (pBackup != None):
		try:
			pList = App.PriorityListAI_Create(pShip, "ClaudeManeuver")
			pList.SetInterruptable(1)
			pList.AddAI(pLeaf, 1)
			pList.AddAI(pBackup, 2)
			pInner = pList
		except:
			pInner = pLeaf
	elif pLeaf != None:
		pInner = pLeaf
	else:
		pInner = pBackup
		DebugPrint("DEGRADED manoeuvre: leaf AI unavailable, flying straight")

	if iKind != MAN_BREAK:
		try:
			pInner = WrapFire(pShip, pInner, sTargetName, g_lFleet[iSlot]["tsub"])
		except:
			DebugPrint("FireScript wrap failed - circling with guns cold: " + SafeErr())

	if not bNoAvoid:
		try:
			pInner = WrapAvoid(pShip, pInner)
		except:
			DebugPrint("AvoidObstacles wrap failed: " + SafeErr())

	return pInner


def AddManeuverTimer(fSeconds, iGeneration):
	# TGIntEvent carries the generation; the handler maps it back to the
	# slot.  If TGIntEvent is unavailable the watchdog ends the manoeuvre.
	pCurGame = App.Game_GetCurrentGame()
	if pCurGame == None:
		return 0
	try:
		pEvent = App.TGIntEvent_Create()
		pEvent.SetEventType(App.ET_CLAUDE_MANEUVER_END)
		pEvent.SetDestination(pCurGame)
		pEvent.SetInt(iGeneration)
		pTimer = App.TGTimer_Create()
		pTimer.SetTimerStart(App.g_kUtopiaModule.GetGameTime() + fSeconds)
		pTimer.SetDelay(0)
		pTimer.SetDuration(0)
		pTimer.SetEvent(pEvent)
		App.g_kTimerManager.AddTimer(pTimer)
	except:
		DebugPrint("manoeuvre timer unavailable, watchdog will end it: " + SafeErr())
		return 0
	return 1


def ClampSeconds(iArg, iDef, iMin, iMax):
	if iArg <= 0:
		iArg = iDef
	if iArg < iMin:
		iArg = iMin
	if iArg > iMax:
		iArg = iMax
	return iArg


##############################################################################
# WARP BREAK (order 18, 2026-10-24)
#
# A BREAK flees at impulse and the chaser keeps pace ("BREAK NOT OPENING"),
# so a hurt ship never buys the time to mend.  The player gets out with
# In-System Warp: intercept a nav point far away and the engine warps there.
# Same trick here: plant a nav point WARP_DIST units beyond the ship on the
# line away from its target, Intercept it (warps beyond WARP_TRIGGER),
# then Stay there with shields power + the repair bay until the mend time
# runs out, the shields are back, or someone follows.  Ending is the normal
# EndManeuver -> attack AI, and the attack AI's own Intercept warps back in
# because the target is now far beyond the stock 295-unit warp distance.
##############################################################################

MAN_WARP = 18			# telemetry Man code while a warp break runs
WARP_DIST = 1500.0		# game units from the target to the mend point
WARP_TRIGGER = 200.0		# Intercept warps when its target is farther
WARP_ARRIVE = 150.0		# this close to the mend point = arrived
WARP_OUT_MAX = 60		# ticks to get the warp going before giving up -
				# under fire it can take a while to engage (the player:
				# "the ship has to stick with it and it will go")
WARP_KEEPALIVE = 15		# each tick spent AT warp pushes the give-up this far
				# out, so an interrupted warp keeps its retries
WARP_MEND_DEF = 30
WARP_MEND_MIN = 10
WARP_MEND_MAX = 90
WARP_MIN_HULL = 25.0		# % - InSystemWarp's own "too wrecked to warp"
WARP_CHASER = 400.0		# an enemy this close while mending ends it
WARP_DONE_SHIELDS = 90		# mend ends early once shields average this


def WarpNav(pShip):
	# One invisible waypoint per ship, found again by name (never a pointer
	# in module state).  Not a nav point, so it stays off the player's list.
	try:
		pSet = pShip.GetContainingSet()
		if pSet == None:
			return None
		sName = "ClaudeWarpBreak_" + pShip.GetName()
		pNav = App.Waypoint_Cast(App.ObjectClass_GetObject(pSet, sName))
		if pNav == None:
			pNav = App.Waypoint_Create(sName, pSet.GetName(), None)
			# First live test (10-24): four warp breaks, no warp - the
			# ship crawled at impulse.  InSystemWarp.py marks its point
			# SetNavPoint(1) and the engine only warps to those, it seems.
			try:
				pNav.SetNavPoint(1)
			except:
				DebugPrint("warp nav: SetNavPoint unavailable: " + SafeErr())
		return pNav
	except:
		DebugPrint("warp nav failed: " + SafeErr())
		return None


def CanWarpBreak(pShip):
	# InSystemWarp.CanWarp's rule: hull above 25%, warp AND impulse working.
	try:
		if Pct(Health(pShip.GetHull())) <= WARP_MIN_HULL:
			return "hull too low to warp"
		pWarp = pShip.GetWarpEngineSubsystem()
		if (pWarp == None) or pWarp.IsDisabled():
			return "warp engines down"
		pImp = pShip.GetImpulseEngineSubsystem()
		if (pImp == None) or pImp.IsDisabled():
			return "impulse engines down"
	except:
		return "warp check failed"
	return ""


def MakeWarpRun(pShip, sNavName):
	# Intercept the nav point, with Stay behind it so the ship is never
	# AI-less when Intercept reports DONE on arrival (the watchdog would
	# read that as "no AI" and end the break before the mend began).
	pRun = App.PlainAI_Create(pShip, "ClaudeWarpRun")
	pRun.SetScriptModule("Intercept")
	pRun.SetInterruptable(1)
	pScript = pRun.GetScriptInstance()
	pScript.SetTargetObjectName(sNavName)
	pScript.SetInterceptDistance(WARP_ARRIVE / 2.0)
	pScript.SetInSystemWarpDistance(WARP_TRIGGER)
	pList = App.PriorityListAI_Create(pShip, "ClaudeWarpBreak")
	pList.SetInterruptable(1)
	pList.AddAI(pRun, 1)
	pList.AddAI(MakeWarpHold(pShip), 2)
	return pList


def MakeWarpHold(pShip):
	pHold = App.PlainAI_Create(pShip, "ClaudeWarpHold")
	pHold.SetScriptModule("Stay")
	pHold.SetInterruptable(1)
	return pHold


def ShieldAverage(pShip):
	try:
		pShields = pShip.GetShields()
		fCur = 0.0
		fMax = 0.0
		for sLabel, eFace in ShieldFacings():
			fCur = fCur + pShields.GetCurShields(eFace)
			fMax = fMax + pShields.GetMaxShields(eFace)
		if fMax > 0.0:
			return int(100.0 * fCur / fMax)
	except:
		pass
	return -1


def StartWarpBreak(iSlot, iMendSecs):
	global g_iManGen
	pShip = FleetShip(iSlot)
	if pShip == None:
		return "no ship in slot " + str(iSlot)
	dSlot = g_lFleet[iSlot]
	if dSlot["man"] == "warp":
		return "warp break already running"
	iCloak = CloakCode(pShip)
	if (iCloak == 2) or (iCloak == 4):
		return "cloak changing - reissue in a moment"
	sWhy = CanWarpBreak(pShip)
	if sWhy != "":
		return "warp break refused: " + sWhy
	try:
		if pShip.HasBuildingAIs():
			return "AI still building - reissue in a moment"
	except:
		pass
	iCode, pThreat = ResolveTarget(iSlot)
	if pThreat == None:
		return "no target to warp away from"

	kMe = pShip.GetWorldLocation()
	kThreat = pThreat.GetWorldLocation()
	vAway = App.TGPoint3()
	vAway.SetXYZ(kMe.GetX() - kThreat.GetX(),
		     kMe.GetY() - kThreat.GetY(),
		     kMe.GetZ() - kThreat.GetZ())
	if vAway.Unitize() < 1.0:
		try:
			vFwd = pShip.GetWorldForwardTG()
			vAway.SetXYZ(0.0 - vFwd.GetX(), 0.0 - vFwd.GetY(), 0.0 - vFwd.GetZ())
		except:
			vAway.SetXYZ(0.0, 0.0, 1.0)
	pNav = WarpNav(pShip)
	if pNav == None:
		return "warp break: no nav point"
	try:
		pNav.SetTranslateXYZ(kThreat.GetX() + vAway.GetX() * WARP_DIST,
				     kThreat.GetY() + vAway.GetY() * WARP_DIST,
				     kThreat.GetZ() + vAway.GetZ() * WARP_DIST)
	except:
		return "warp break: could not place the nav point"

	pAI = None
	try:
		pAI = MakeWarpRun(pShip, pNav.GetName())
	except:
		pAI = None
		DebugPrint("warp run build FAILED: " + SafeErr())
	if pAI == None:
		dSlot["manFail"] = 1
		return "warp break build FAILED (still attacking)"
	try:
		pShip.SetAI(pAI, 0, 0)
	except:
		dSlot["manFail"] = 1
		DebugPrint("SetAI(warp) FAILED: " + SafeErr())
		EndManeuver(iSlot, "warp setai failed")
		return "warp break SetAI FAILED"

	g_iManGen = g_iManGen + 1
	dSlot["man"] = "warp"
	dSlot["manGen"] = g_iManGen
	dSlot["manStart"] = g_iTick
	dSlot["manEnd"] = g_iTick + WARP_OUT_MAX + iMendSecs + 5
	dSlot["manRange0"] = int(Range(pShip, pThreat))
	dSlot["manFail"] = 0
	dSlot["manNoAvoid"] = 0
	dSlot["needRestore"] = 1
	dSlot["wbPhase"] = 1
	dSlot["wbOutEnd"] = g_iTick + WARP_OUT_MAX
	dSlot["wbMend"] = iMendSecs
	dSlot["wbMendEnd"] = 0
	dSlot["wbWarped"] = 0
	dSlot["wbWasWarping"] = 0
	dSlot["wbStarts"] = 0
	AddManeuverTimer(float(dSlot["manEnd"] - g_iTick) + 0.25, g_iManGen)
	DebugPrint("[warp] S" + str(iSlot) + " warp break gen=" + str(g_iManGen) +
		   " from " + pThreat.GetName() + " range0=" + str(dSlot["manRange0"]) +
		   " mend " + str(iMendSecs) + "s")
	return "warp break: running " + str(int(WARP_DIST)) + " out, mend " + str(iMendSecs) + "s"


def ShipSpeed(pShip):
	# (current speed, impulse max) in game units/s; -1.0 when unreadable.
	# player feedback: "look at speed - that is the teller, it'll go 9 then 9+".
	fSpd = -1.0
	fMax = -1.0
	try:
		fSpd = pShip.GetVelocityTG().Length()
	except:
		fSpd = -1.0
	try:
		fMax = pShip.GetImpulseEngineSubsystem().GetMaxSpeed()
	except:
		fMax = -1.0
	return fSpd, fMax


def WarpWatchdog(iSlot, pMine):
	# Phase 1 (out): wait to arrive, give up after WARP_OUT_MAX ticks.
	# Phase 2 (mend): hold, shields power + repair, until done.
	dSlot = g_lFleet[iSlot]
	bWarping = 0
	try:
		bWarping = pMine.IsDoingInSystemWarp()
	except:
		bWarping = 0
	fSpd, fMax = ShipSpeed(pMine)
	if (not bWarping) and (fMax > 0.0) and (fSpd > fMax * 1.5):
		bWarping = 1		# well past impulse max = at warp, whatever the flag says
	if bWarping and not dSlot["wbWarped"]:
		dSlot["wbWarped"] = 1
		DebugPrint("[warp] S" + str(iSlot) + " is at warp")
	if bWarping != dSlot["wbWasWarping"]:
		if dSlot["wbWarped"] and not bWarping and dSlot["wbPhase"] == 1:
			DebugPrint("[warp] S" + str(iSlot) + " dropped out of warp")
		elif bWarping and dSlot["wbStarts"] > 0:
			DebugPrint("[warp] S" + str(iSlot) + " back at warp (attempt " +
				   str(dSlot["wbStarts"] + 1) + ")")
		if bWarping:
			dSlot["wbStarts"] = dSlot["wbStarts"] + 1
		dSlot["wbWasWarping"] = bWarping
	if bWarping and dSlot["wbPhase"] == 1:
		# Stick with it: while the warp is actually running, keep the
		# give-up and the overall deadline ahead of us.
		if dSlot["wbOutEnd"] < g_iTick + WARP_KEEPALIVE:
			dSlot["wbOutEnd"] = g_iTick + WARP_KEEPALIVE
		if dSlot["manEnd"] < dSlot["wbOutEnd"] + dSlot["wbMend"] + 5:
			dSlot["manEnd"] = dSlot["wbOutEnd"] + dSlot["wbMend"] + 5

	if dSlot["wbPhase"] == 1:
		pNav = WarpNav(pMine)
		fToNav = -1.0
		if pNav != None:
			fToNav = Range(pMine, pNav)
		bThere = (fToNav >= 0.0) and (fToNav <= WARP_ARRIVE)
		if (not bThere) and dSlot["wbWarped"] and (not bWarping) and \
		   (fToNav >= 0.0) and (fToNav <= WARP_ARRIVE * 4.0):
			bThere = 1		# dropped out a little short: good enough
		if ((g_iTick - dSlot["manStart"]) % 5) == 0:
			DebugPrint("[warp] S" + str(iSlot) + " out: " + str(int(fToNav)) +
				   " to the point, warping=" + str(bWarping) + " speed=" +
				   str(int(fSpd)) + " (impulse max " + str(int(fMax)) + ")")
		if bThere and not bWarping:
			try:
				pMine.SetAI(MakeWarpHold(pMine), 0, 0)
			except:
				DebugPrint("warp hold FAILED: " + SafeErr())
			try:
				DoPower(iSlot, "shields")
				DoRepair(iSlot)
			except:
				pass
			dSlot["wbPhase"] = 2
			dSlot["wbMendEnd"] = g_iTick + dSlot["wbMend"]
			dSlot["manEnd"] = dSlot["wbMendEnd"] + 2
			DebugPrint("[warp] S" + str(iSlot) + " arrived (" + str(int(fToNav)) +
				   " from the point" + (" after warp" * dSlot["wbWarped"]) +
				   ") - mending " + str(dSlot["wbMend"]) + "s")
			return
		if g_iTick >= dSlot["wbOutEnd"]:
			dSlot["manFail"] = 2
			sWhy = "warp never engaged"
			if dSlot["wbWarped"]:
				sWhy = "never reached the mend point"
			DebugPrint("[warp] S" + str(iSlot) + " " + sWhy + " - back to the attack")
			EndManeuver(iSlot, sWhy)
		return

	if dSlot["wbPhase"] == 2:
		sDone = ""
		if g_iTick >= dSlot["wbMendEnd"]:
			sDone = "mend time up"
		else:
			iSh = ShieldAverage(pMine)
			if iSh >= WARP_DONE_SHIELDS:
				sDone = "shields back at " + str(iSh) + "%"
			else:
				iCode, pThreat = ResolveTarget(iSlot)
				if pThreat != None:
					fNear = Range(pMine, pThreat)
					if (fNear >= 0.0) and (fNear < WARP_CHASER):
						sDone = "followed - " + pThreat.GetName() + " at " + str(int(fNear))
		if sDone != "":
			try:
				DoPower(iSlot, "balanced")
			except:
				pass
			DebugPrint("[warp] S" + str(iSlot) + " mend over (" + sDone + ") - back to the fight")
			EndManeuver(iSlot, "warp mend over: " + sDone)


def SparTarget(iSlot):
	# (code, ship) for the nearest live ship of the OPPOSING FLEET, the code
	# being SPAR_TGT_BASE + its slot so the outside reader can find it.
	dSlot = g_lFleet[iSlot]
	pMine = FleetShip(iSlot)
	if pMine == None:
		return (FOCUS_NONE, None)
	iBest = FOCUS_NONE
	pBest = None
	fBest = -1.0
	j = 0
	while j < MAX_FLEET:
		if (j != iSlot) and (g_lFleet[j]["st"] == SLOT_ALIVE) and (g_lFleet[j]["side"] != dSlot["side"]):
			pOther = FleetShip(j)
			if pOther != None:
				fRange = Range(pMine, pOther)
				if (fRange >= 0.0) and ((fBest < 0.0) or (fRange < fBest)):
					fBest = fRange
					iBest = SPAR_TGT_BASE + j
					pBest = pOther
		j = j + 1
	return (iBest, pBest)


def ResolveTarget(iSlot):
	# What should this ship be attacking right now?
	# Returns (iCode, pShip): iCode in 0..MAX_FOES-1, FOCUS_PLAYER, or
	# FOCUS_NONE with pShip None.  Fallback when no focus: nearest live
	# opposing ship (the player counts as opposing for enemy-side ships).
	dSlot = g_lFleet[iSlot]
	iFocus = dSlot["focus"]

	if iFocus == FOCUS_SPAR:
		# Sparring measures against the OPPOSING FLEET only.  This used to
		# fall through to the player for red and to nothing for blue, so
		# spar Rng/Closing/Brg - and break/circle/guard - were never about
		# the ship actually being fought.
		return SparTarget(iSlot)
	if iFocus == FOCUS_PLAYER:
		pPlayer = App.Game_GetCurrentPlayer()
		if pPlayer != None:
			return (FOCUS_PLAYER, pPlayer)
	elif (iFocus >= 0) and (iFocus < MAX_FOES):
		pFoe = FoeShip(iFocus)
		if pFoe != None:
			# Only a foe on the OTHER side is a legal target.
			if g_lFoes[iFocus]["side"] != dSlot["side"]:
				return (iFocus, pFoe)
	elif (iFocus >= SPAR_TGT_BASE) and (iFocus < SPAR_TGT_BASE + MAX_FLEET):
		j = iFocus - SPAR_TGT_BASE
		if (j != iSlot) and (g_lFleet[j]["st"] == SLOT_ALIVE) and (g_lFleet[j]["side"] != dSlot["side"]):
			pOther = FleetShip(j)
			if pOther != None:
				return (iFocus, pOther)

	# No (valid) focus: nearest live opposing ship.
	pMine = FleetShip(iSlot)
	if pMine == None:
		return (FOCUS_NONE, None)
	iBest = FOCUS_NONE
	pBest = None
	fBest = -1.0
	j = 0
	while j < MAX_FOES:
		if (g_lFoes[j]["st"] == SLOT_ALIVE) and (g_lFoes[j]["side"] != dSlot["side"]):
			pFoe = FoeShip(j)
			if pFoe != None:
				fRange = Range(pMine, pFoe)
				if (fRange >= 0.0) and ((fBest < 0.0) or (fRange < fBest)):
					fBest = fRange
					iBest = j
					pBest = pFoe
		j = j + 1
	if dSlot["side"] == 0:
		pPlayer = App.Game_GetCurrentPlayer()
		if pPlayer != None:
			fRange = Range(pMine, pPlayer)
			if (fRange >= 0.0) and ((fBest < 0.0) or (fRange < fBest)):
				iBest = FOCUS_PLAYER
				pBest = pPlayer
	return (iBest, pBest)


def StartManeuver(iSlot, iKind, iSeconds, bNoAvoid = 0):
	global g_iManGen

	pShip = FleetShip(iSlot)
	if pShip == None:
		return "no ship in slot " + str(iSlot)
	dSlot = g_lFleet[iSlot]

	iCode, pTarget = ResolveTarget(iSlot)
	if pTarget == None:
		return "no target to manoeuvre against"
	try:
		sTargetName = pTarget.GetName()
	except:
		return "target unnameable"

	# Stock code refuses to replace an AI while the tree is still building.
	try:
		if pShip.HasBuildingAIs():
			return "AI still building - reissue in a moment"
	except:
		pass

	iRangeNow = int(Range(pShip, pTarget))

	pAI = None
	try:
		pAI = BuildManeuverAI(pShip, iSlot, iKind, sTargetName, iRangeNow, bNoAvoid)
	except:
		pAI = None
		DebugPrint("BuildManeuverAI raised: " + SafeErr())

	if pAI == None:
		dSlot["manFail"] = 1
		DebugPrint("MANEUVER BUILD FAILED slot=" + str(iSlot) + " kind=" + str(iKind))
		Announce("Claude: manoeuvre unavailable - pressing the attack.")
		return "manoeuvre build FAILED (still attacking)"

	try:
		pShip.SetAI(pAI, 0, 0)
	except:
		dSlot["manFail"] = 1
		DebugPrint("SetAI(manoeuvre) FAILED: " + SafeErr())
		Announce("Claude: helm refused the manoeuvre.")
		EndManeuver(iSlot, "setai failed")
		return "manoeuvre SetAI FAILED"

	if iKind == MAN_CIRCLE:
		dSlot["man"] = "circle"
	elif iKind == MAN_GUARD:
		dSlot["man"] = "guard"
	else:
		dSlot["man"] = "break"
	g_iManGen = g_iManGen + 1
	dSlot["manGen"] = g_iManGen
	dSlot["manStart"] = g_iTick
	dSlot["manEnd"] = g_iTick + iSeconds
	dSlot["manRange0"] = iRangeNow
	dSlot["manFail"] = 0
	dSlot["manNoAvoid"] = bNoAvoid
	dSlot["needRestore"] = 1

	AddManeuverTimer(float(iSeconds) + 0.25, g_iManGen)
	DebugPrint("[man] S" + str(iSlot) + " " + dSlot["man"] + " " + str(iSeconds) +
		   "s gen=" + str(g_iManGen) + " vs " + sTargetName + " range0=" + str(iRangeNow))
	return dSlot["man"] + " for " + str(iSeconds) + "s"


def EndManeuver(iSlot, sWhy):
	# The ONLY way back to the attack AI.  Rebuilds from the module rather
	# than restoring a stashed pointer.
	dSlot = g_lFleet[iSlot]
	if ApplyStance(iSlot, dSlot["stance"]):
		DebugPrint("[man] S" + str(iSlot) + " attack resumed (" + sWhy + ")")
		return 1
	if ApplyStance(iSlot, "balanced"):
		DebugPrint("[man] S" + str(iSlot) + " attack resumed on BALANCED fallback (" + sWhy + ")")
		return 1

	dSlot["man"] = ""
	dSlot["manEnd"] = 0
	dSlot["manStart"] = 0
	dSlot["manRange0"] = -1
	dSlot["needRestore"] = 1
	DebugPrint("S" + str(iSlot) + " ATTACK RESTORE FAILED (" + sWhy + ") - retrying every tick")
	return 0


def ManeuverEndHandler(pObject, pEvent):
	if g_bActive:
		iGen = -1
		try:
			iGen = pEvent.GetInt()
		except:
			iGen = -1
		# Unreadable payload: do nothing - each slot's watchdog enforces
		# its own deadline a tick later anyway, and ending ALL manoeuvres
		# on a garbled event would cut healthy ones short.
		if iGen >= 0:
			i = 0
			while i < MAX_FLEET:
				dSlot = g_lFleet[i]
				# A warp break moves its own deadline (stick with a warp
				# that is being impeded), so its start-time timer only ends
				# it once that deadline has really passed.
				if (dSlot["st"] == SLOT_ALIVE) and (dSlot["man"] != "") and (dSlot["manGen"] == iGen) and \
				   ((dSlot["man"] != "warp") or (g_iTick >= dSlot["manEnd"])):
					try:
						EndManeuver(i, "timer")
					except:
						DebugPrint("EndManeuver raised in timer: " + SafeErr())
				i = i + 1
	pObject.CallNextHandler(pEvent)


def HasAI(pShip):
	try:
		if pShip.GetAI() == None:
			return 0
	except:
		return 1
	return 1


def ManeuverWatchdog(iSlot):
	# Every tick per live slot: pay off an owed restore, resurrect a ship
	# with no AI, enforce the deadline, detect an ignored manoeuvre.
	dSlot = g_lFleet[iSlot]

	if (dSlot["man"] == "") and dSlot["needRestore"]:
		EndManeuver(iSlot, "restore retry")
		return

	pMine = FleetShip(iSlot)
	if pMine == None:
		return

	bBuilding = 0
	try:
		if pMine.HasBuildingAIs():
			bBuilding = 1
	except:
		bBuilding = 0

	if (not bBuilding) and (not HasAI(pMine)):
		dSlot["manFail"] = 3
		dSlot["noAIWarn"] = dSlot["noAIWarn"] + 1
		DebugPrint("S" + str(iSlot) + " HAS NO AI (warn " + str(dSlot["noAIWarn"]) + ") - forcing rebuild")
		EndManeuver(iSlot, "no ai")
		return
	if not bBuilding:
		dSlot["noAIWarn"] = 0

	if dSlot["man"] == "":
		return

	if g_iTick >= dSlot["manEnd"]:
		EndManeuver(iSlot, "deadline")
		return

	if dSlot["man"] == "warp":
		WarpWatchdog(iSlot, pMine)
		return

	# Effectiveness verdict, once at the grace boundary.
	if (g_iTick - dSlot["manStart"]) != MAN_GRACE_TICKS:
		return
	if dSlot["manRange0"] < 0:
		return
	iCode, pTarget = ResolveTarget(iSlot)
	if pTarget == None:
		return
	iNow = int(Range(pMine, pTarget))
	if iNow < 0:
		return

	iDelta = iNow - dSlot["manRange0"]
	if dSlot["man"] == "break":
		bHard = 0
		if iDelta <= (0 - BREAK_HARD_CLOSE):
			bHard = 1
		if bHard or (iDelta < BREAK_WANT_OPEN):
			dSlot["manFail"] = 2
			# A break that is not opening range is almost always the
			# AvoidObstacles wrapper parking the ship (see ManeuverAvoid).
			# Round 8 only LOGGED this verdict and let the useless break
			# run its full duration while the ship was shot apart, so now
			# we re-arm it without the avoider and re-judge after another
			# grace window.  manNoAvoid stops this from looping.
			if not dSlot["manNoAvoid"]:
				iLeft = dSlot["manEnd"] - g_iTick
				if iLeft < BREAK_MIN_SEC:
					iLeft = BREAK_MIN_SEC
				DebugPrint("S" + str(iSlot) + " BREAK NOT OPENING: " +
					   str(dSlot["manRange0"]) + " -> " + str(iNow) +
					   " - retrying without obstacle avoidance")
				StartManeuver(iSlot, MAN_BREAK, iLeft, 1)
				return
			if bHard:
				DebugPrint("S" + str(iSlot) + " BREAK INEFFECTIVE even unavoided: " +
					   str(dSlot["manRange0"]) + " -> " + str(iNow) + " - aborting")
				EndManeuver(iSlot, "break ineffective")
				return
			DebugPrint("S" + str(iSlot) + " BREAK WEAK (unavoided): " +
				   str(dSlot["manRange0"]) + " -> " + str(iNow))
	else:
		# Judge orbits against the standoff BAND, not range-at-issue.
		iFloor = int(CIRCLE_NEAR) - 60
		if iNow < iFloor:
			if iNow < (iFloor / 2):
				dSlot["manFail"] = 2
				DebugPrint("S" + str(iSlot) + " ORBIT INEFFECTIVE: range " + str(iNow) +
					   " far inside floor " + str(iFloor) + " - aborting")
				EndManeuver(iSlot, "orbit ineffective")
				return
			dSlot["manFail"] = 2
			DebugPrint("S" + str(iSlot) + " ORBIT TIGHT: range " + str(iNow) +
				   " under floor " + str(iFloor))


##############################################################################
# Subsystem helpers (unchanged from v2)
##############################################################################

def WeakestFacing(pShip):
	# Lowest live shield facing as a percent, or 101 if the ship has no
	# shields at all (so it can never trip the facing test).
	iWorst = 101
	try:
		pShields = pShip.GetShields()
	except:
		pShields = None
	if pShields == None:
		return iWorst
	for sLabel, eFacing in ShieldFacings():
		try:
			fMax = pShields.GetMaxShields(eFacing)
			if fMax > 0.0:
				iThis = int(pShields.GetCurShields(eFacing) * 100.0 / fMax)
				if iThis < iWorst:
					iWorst = iThis
		except:
			pass
	return iWorst


def AutoBreakOne(iSlot):
	dSlot = g_lFleet[iSlot]
	pShip = FleetShip(iSlot)
	if pShip == None:
		return

	# Keep the damage-rate window fed even while a manoeuvre is running,
	# so the history is meaningful the moment the ship is free again.
	iHull = Pct(Health(pShip.GetHull()))
	lHist = dSlot["abHist"]
	iDrop = 0
	if iHull >= 0:
		lHist.append(iHull)
		while len(lHist) > (AB_WINDOW + 1):
			del lHist[0]
		# Only meaningful once we have a real reading to compare against.
		# Pct() returns -1 for an unreadable hull, and differencing that
		# against a live history entry would look like catastrophic damage
		# and fire the reflex for no reason.
		if len(lHist) > 1:
			iDrop = lHist[0] - iHull

	if dSlot["man"] != "":
		return
	if dSlot["stance"] == "free":
		return
	if g_iTick < dSlot["abNext"]:
		return

	iFace = WeakestFacing(pShip)
	sWhy = ""
	if (iHull >= 0) and (iHull <= dSlot["abHull"]):
		sWhy = "hull " + str(iHull) + "%"
	elif iFace <= dSlot["abFace"]:
		sWhy = "shield facing " + str(iFace) + "%"
	elif iDrop >= dSlot["abDrop"]:
		sWhy = "lost " + str(iDrop) + " hull in " + str(AB_WINDOW) + "s"
	if sWhy == "":
		return

	# Arm the cooldown BEFORE acting: hull does not climb back quickly, so
	# without this the hull test would re-fire the instant the break ended
	# and the ship would run for the rest of the battle.
	dSlot["abNext"] = g_iTick + dSlot["abSecs"] + AB_COOLDOWN
	dSlot["abFires"] = dSlot["abFires"] + 1
	dSlot["abHist"] = []

	sRes = StartManeuver(iSlot, MAN_BREAK, dSlot["abSecs"])
	try:
		DoCloak(iSlot, 1)
	except:
		pass
	try:
		DoPower(iSlot, "shields")
	except:
		pass
	DebugPrint("[autobreak] S" + str(iSlot) + " " + sWhy + " -> " + sRes)
	Announce(dSlot["name"] + " is breaking off - " + sWhy + ".")


def CheckAutoBreak():
	i = 0
	while i < MAX_FLEET:
		dSlot = g_lFleet[i]
		if dSlot["abOn"] and (dSlot["st"] == SLOT_ALIVE):
			try:
				AutoBreakOne(i)
			except:
				DebugPrint("autobreak S" + str(i) + " raised: " + SafeErr())
		i = i + 1


def DoAutoBreak(iSlot, iArg, sText):
	dSlot = g_lFleet[iSlot]
	if iArg == 0:
		dSlot["abOn"] = 0
		return "autobreak off"
	# Text is "hull,facing,drop,secs" - any field may be blank or absent.
	lKeys = ( "abHull", "abFace", "abDrop", "abSecs" )
	lVals = string.split(string.strip(sText), ",")
	j = 0
	while j < len(lKeys):
		if j < len(lVals):
			sVal = string.strip(lVals[j])
			if sVal != "":
				try:
					dSlot[lKeys[j]] = int(sVal)
				except:
					pass
		j = j + 1
	dSlot["abSecs"] = ClampSeconds(dSlot["abSecs"], BREAK_DEF_SEC,
				       BREAK_MIN_SEC, BREAK_MAX_SEC)
	dSlot["abOn"] = 1
	dSlot["abNext"] = 0
	dSlot["abHist"] = []
	return ("autobreak armed: hull<=" + str(dSlot["abHull"]) +
		"% / facing<=" + str(dSlot["abFace"]) +
		"% / -" + str(dSlot["abDrop"]) + "hull per " + str(AB_WINDOW) +
		"s -> break " + str(dSlot["abSecs"]) + "s + cloak + shields")


def GetSubsystem(pShip, sKeyword):
	sKeyword = string.lower(string.strip(sKeyword))
	try:
		if sKeyword == "hull":
			return pShip.GetHull()
		if sKeyword == "shields":
			return pShip.GetShields()
		if (sKeyword == "weapons") or (sKeyword == "phasers"):
			return pShip.GetPhaserSystem()
		if (sKeyword == "torps") or (sKeyword == "torpedoes"):
			return pShip.GetTorpedoSystem()
		if (sKeyword == "engines") or (sKeyword == "impulse"):
			return pShip.GetImpulseEngineSubsystem()
		if sKeyword == "warp":
			return pShip.GetWarpEngineSubsystem()
		if sKeyword == "sensors":
			return pShip.GetSensorSubsystem()
		if sKeyword == "power":
			return pShip.GetPowerSubsystem()
		if sKeyword == "repair":
			return pShip.GetRepairSubsystem()
		if sKeyword == "cloak":
			return pShip.GetCloakingSubsystem()
	except:
		return None
	return None


def Pct(fValue):
	if fValue < 0.0:
		return -1
	return int(fValue * 100.0)


def Health(pSubsystem):
	if pSubsystem == None:
		return -1.0
	try:
		return pSubsystem.GetConditionPercentage()
	except:
		return -1.0


def ShieldFacings():
	lOut = []
	lNames = ( ("F", "FRONT_SHIELDS"), ("A", "REAR_SHIELDS"),
		   ("T", "TOP_SHIELDS"), ("B", "BOTTOM_SHIELDS"),
		   ("L", "LEFT_SHIELDS"), ("R", "RIGHT_SHIELDS") )
	for sLabel, sConst in lNames:
		try:
			lOut.append((sLabel, getattr(App.ShieldClass, sConst)))
		except:
			pass
	return lOut


def CloakCode(pShip):
	# 0 no device, 1 ready, 2 cloaking, 3 cloaked, 4 decloaking.
	# IsDecloaking is tested FIRST: the engine appears to keep IsCloaked()
	# true through part of the decloak ramp, and the old order reported 3
	# (CLOAKED) for that whole window - round 5's misleading telemetry.
	try:
		pCloak = pShip.GetCloakingSubsystem()
		if pCloak == None:
			return 0
		if pCloak.IsDecloaking():
			return 4
		if pCloak.IsCloaking():
			return 2
		if pCloak.IsCloaked():
			return 3
		return 1
	except:
		return 0


def ChildHealth(pSub):
	# (worst child condition %, children, children disabled) - (-1, 0, 0)
	# when the system has no children or cannot be read.
	iWorst = -1
	iKids = 0
	iOut = 0
	if pSub == None:
		return iWorst, iKids, iOut
	try:
		iKids = pSub.GetNumChildSubsystems()
	except:
		return -1, 0, 0
	i = 0
	while i < iKids:
		pChild = None
		try:
			pChild = pSub.GetChildSubsystem(i)
		except:
			pChild = None
		if pChild != None:
			iThis = Pct(Health(pChild))
			if (iThis >= 0) and ((iWorst < 0) or (iThis < iWorst)):
				iWorst = iThis
			try:
				if pChild.IsDisabled():
					iOut = iOut + 1
			except:
				pass
		i = i + 1
	return iWorst, iKids, iOut


def ShipReport(sPrefix, pShip, lInts):
	# Append (key, int) telemetry pairs for one ship.
	lInts.append((sPrefix + "Hull", Pct(Health(pShip.GetHull()))))

	try:
		pShields = pShip.GetShields()
	except:
		pShields = None
	if pShields != None:
		try:
			lInts.append((sPrefix + "ShAvg", Pct(pShields.GetShieldPercentage())))
		except:
			pass
		for sLabel, eFacing in ShieldFacings():
			try:
				fMax = pShields.GetMaxShields(eFacing)
				fCur = pShields.GetCurShields(eFacing)
				if fMax > 0.0:
					lInts.append((sPrefix + "Sh" + sLabel, int(fCur * 100.0 / fMax)))
			except:
				pass

	lSystems = ( ("Weap", "weapons"), ("Torp", "torps"), ("Eng", "engines"),
		     ("Warp", "warp"), ("Sens", "sensors"), ("Pow", "power") )
	for sKey, sName in lSystems:
		pSub = GetSubsystem(pShip, sName)
		iHealth = Pct(Health(pSub))
		if (sKey == "Eng") or (sKey == "Warp"):
			# The parent reads 100% while its engines burn: on a Fek'lhr
			# "Impulse Engines" is not even targetable - the player shoots
			# "Impulse 1"/"Impulse 2", and two logged duels read Eng 100 the
			# whole way down.  Report the worst of parent and children,
			# plus how many children there are and how many are out.
			iWorst, iKids, iOut = ChildHealth(pSub)
			if (iWorst >= 0) and ((iHealth < 0) or (iWorst < iHealth)):
				iHealth = iWorst
			lInts.append((sPrefix + sKey + "N", iKids))
			lInts.append((sPrefix + sKey + "Out", iOut))
			if sKey == "Eng":
				try:
					pRepair = pShip.GetRepairSubsystem()
				except:
					pRepair = None
				iOn = EngineRepairCount(pShip, pRepair)
				if iOn >= 0:
					lInts.append((sPrefix + "EngRep", iOn))
		if iHealth >= 0:
			lInts.append((sPrefix + sKey, iHealth))

	# Impulse top speed x10: what the drive can do NOW against what it was
	# built for.  The teller for "she cannot manoeuvre" (player feedback: "a
	# behemoth is taken down easily when it can't manoeuvre").
	try:
		pImp = pShip.GetImpulseEngineSubsystem()
		if pImp != None:
			lInts.append((sPrefix + "ImpMax", int(pImp.GetMaxSpeed() * 10.0)))
			lInts.append((sPrefix + "ImpFull",
				      int(pImp.GetProperty().GetMaxSpeed() * 10.0)))
	except:
		pass
	try:
		lInts.append((sPrefix + "Spd10",
			      int(pShip.GetVelocityTG().Length() * 10.0)))
	except:
		pass

	# Main battery reserve percent (PowerDisplay.py:864-867 call shape).
	# Cloak instrumentation: a Vor'cha cloak drains 700 power/s, and the
	# round-5 "early decloak" is suspected power exhaustion - watch this.
	try:
		pPower = pShip.GetPowerSubsystem()
		if pPower != None:
			fLimit = pPower.GetProperty().GetMainBatteryLimit()
			if fLimit > 0.0:
				lInts.append((sPrefix + "Batt",
					int(pPower.GetMainBatteryPower() / fLimit * 100.0)))
	except:
		pass

	# Repair telemetry.  Claude was completely blind to this until
	# 2026-08-05 - the player has been out-repairing Fable every single round and
	# there was no way to even see it happening, let alone react.
	# Rep    = repair subsystem's own condition (a wrecked repair bay is
	#          why a ship suddenly stops healing)
	# RepPts = max repair points, i.e. the repair RATE this hull gets
	# Dmg    = how many tracked subsystems are below 90%, so "am I actually
	#          getting fixed?" is answerable by watching it fall.
	try:
		pRep = pShip.GetRepairSubsystem()
		if pRep != None:
			iRep = Pct(Health(pRep))
			if iRep >= 0:
				lInts.append((sPrefix + "Rep", iRep))
			try:
				fPts = pRep.GetProperty().GetMaxRepairPoints()
				lInts.append((sPrefix + "RepPts", int(fPts)))
			except:
				pass
	except:
		pass
	iDamaged = 0
	for sKey, sName in lSystems:
		iHealth = Pct(Health(GetSubsystem(pShip, sName)))
		if (iHealth >= 0) and (iHealth < 90):
			iDamaged = iDamaged + 1
	lInts.append((sPrefix + "Dmg", iDamaged))

	lInts.append((sPrefix + "Cloak", CloakCode(pShip)))


def Range(pA, pB):
	try:
		kA = pA.GetWorldLocation()
		kB = pB.GetWorldLocation()
		fX = kA.GetX() - kB.GetX()
		fY = kA.GetY() - kB.GetY()
		fZ = kA.GetZ() - kB.GetZ()
		return math.sqrt(fX * fX + fY * fY + fZ * fZ)
	except:
		return -1.0


def Bearing(pFrom, pTo):
	# Degrees off pFrom's nose towards pTo; 0 = dead ahead.
	try:
		kA = pFrom.GetWorldLocation()
		kB = pTo.GetWorldLocation()
		fX = kB.GetX() - kA.GetX()
		fY = kB.GetY() - kA.GetY()
		fZ = kB.GetZ() - kA.GetZ()
		fDist = math.sqrt(fX * fX + fY * fY + fZ * fZ)
		if fDist <= 0.0:
			return -1.0
		vForward = pFrom.GetWorldForwardTG()
		fDot = (vForward.GetX() * fX + vForward.GetY() * fY + vForward.GetZ() * fZ) / fDist
		if fDot > 1.0:
			fDot = 1.0
		if fDot < -1.0:
			fDot = -1.0
		return math.acos(fDot) * 180.0 / App.PI
	except:
		return -1.0


##############################################################################
# Foe tracking
##############################################################################

def ShipTypeName(pShip):
	try:
		return string.split(pShip.GetScript(), ".")[-1]
	except:
		return ""


def IsFleetName(sName):
	i = 0
	while i < MAX_FLEET:
		if (g_lFleet[i]["st"] != SLOT_EMPTY) and (g_lFleet[i]["name"] == sName):
			return 1
		i = i + 1
	return 0


##############################################################################
# Non-combatant filler
#
# QuickBattle refuses to start with no other ships, so the player pads the roster
# with something harmless - a Decoy, or (2026-08-05) an Asteroid.  The engine
# does not know "harmless": hostility is ObjectGroup membership ONLY, and
# stock missions genuinely do put asteroids in a combat group on purpose
# (E1M2.CreateStartingObjects drops every asteroid into the ENEMY group), so
# an attack AI will happily fly across the map to shoot a rock.
#
# That is exactly what killed the 2026-08-05 duel.  The player was cloaked at the
# moment Fable spawned; SelectTarget.FindGoodTarget drops cloaked ships from
# the candidate list (Preprocessors.py:1446-1450), which left Asteroid-1 as
# the ONLY candidate - and at len(lTargets)==1 it is taken with no scoring at
# all (Preprocessors.py:1462-1464).  Fable flew off to fight the asteroid and
# never came back, because fIsTargetFactor(1.0) + fDistanceFactor(1.0) keep
# the incumbent winning once it is also the closer object.
#
# Fix: evict filler from the mission groups outright.  The object stays in
# the scene as an obstacle, it just stops being something anyone can target -
# which also fixes stock-AI ships doing the same silly thing.  RemoveName is
# stock-proven (AI/Compound/TractorDockTargets.py:62).
##############################################################################

lFillerTypes = [ "Decoy", "Probe", "KessokMine" ]
FILLER_PREFIX = "Asteroid"		# Asteroid, Asteroid1-3, Asteroidh1-3
					# (NOT Astrominer - that one is a real ship)

g_dEvicted = {}


def IsNonCombatant(sType):
	if sType == "":
		return 0
	if sType[:len(FILLER_PREFIX)] == FILLER_PREFIX:
		return 1
	for sFiller in lFillerTypes:
		if sType == sFiller:
			return 1
	return 0


def EvictFiller():
	# Strip non-combatant filler out of the FRIENDLY group so no AI - ours
	# or stock - can select it as a target.
	#
	# Friendly side only, deliberately.  QuickBattle decides the battle on a
	# counter (g_iNumEnemies, bumped at spawn and decremented in the
	# ship-destroyed handler, QuickBattle.py:3235/4054) rather than by
	# walking the group, so removing names cannot break the win check either
	# way.  But an asteroid added on the ENEMY side is a real objective that
	# still has to be destroyed to win, so leave that one shootable; it is
	# only friendly-side padding that has no business being a target.
	global g_dEvicted
	import MissionLib
	pPlayer = App.Game_GetCurrentPlayer()
	if pPlayer == None:
		return
	pSet = pPlayer.GetContainingSet()
	if pSet == None:
		return
	sPlayerName = PlayerName()
	for pGroup in ( MissionLib.GetFriendlyGroup(), ):
		if pGroup == None:
			continue
		lObjects = ()
		try:
			lObjects = pGroup.GetActiveObjectTupleInSet(pSet)
		except:
			lObjects = ()
		for pObject in lObjects:
			# NB: no `continue` inside the try - that is a SyntaxError
			# in Python 1.5.2 even though py_compile accepts it.
			sName = ""
			bFiller = 0
			try:
				pShip = App.ShipClass_Cast(pObject)
				if pShip != None:
					sName = pShip.GetName()
					bFiller = IsNonCombatant(ShipTypeName(pShip))
			except:
				sName = ""
				bFiller = 0
			# Never evict the player: they live in the friendly group,
			# and pulling them out would make them untargetable and
			# unwinnable-against.  Their hull type is never filler, so
			# this is belt-and-braces.
			if sName == sPlayerName:
				bFiller = 0
			if bFiller and (sName != ""):
				try:
					pGroup.RemoveName(sName)
					if not g_dEvicted.has_key(sName):
						g_dEvicted[sName] = 1
						DebugPrint("[filler] " + sName +
							   " evicted from its mission group"
							   " - not a combatant")
				except:
					pass


def UpdateFoes():
	# Enumerate both mission groups in the player's set; every non-fleet,
	# non-player ship gets (or keeps) a foe slot.  Slots are never
	# reassigned; a vanished or dead foe goes SLOT_DEAD and stays there.
	import MissionLib
	pPlayer = App.Game_GetCurrentPlayer()
	if pPlayer == None:
		return
	pSet = pPlayer.GetContainingSet()
	if pSet == None:
		return
	sPlayerName = PlayerName()

	dSeen = {}
	lGroups = ( (0, MissionLib.GetEnemyGroup()), (1, MissionLib.GetFriendlyGroup()) )
	for iSide, pGroup in lGroups:
		if pGroup == None:
			continue
		try:
			lObjects = pGroup.GetActiveObjectTupleInSet(pSet)
		except:
			continue
		for pObject in lObjects:
			pShip = App.ShipClass_Cast(pObject)
			if pShip == None:
				continue
			if pShip.IsDead() or pShip.IsDying():
				continue
			try:
				sName = pShip.GetName()
			except:
				continue
			if sName == sPlayerName:
				continue
			if IsFleetName(sName):
				continue
			sType = ShipTypeName(pShip)
			if IsNonCombatant(sType):
				# The player's harmless QB filler (Decoy, asteroid, ...)
				# - not a combatant, so not worth a foe slot.
				continue
			dSeen[sName] = iSide

	# Update existing slots: revive returnees (WarpOutBeforeDying ships
	# come back!), mark the missing dead.
	dSlotted = {}
	j = 0
	while j < MAX_FOES:
		dFoe = g_lFoes[j]
		if dFoe["st"] != SLOT_EMPTY:
			dSlotted[dFoe["name"]] = j
			if dSeen.has_key(dFoe["name"]):
				if dFoe["st"] == SLOT_DEAD:
					DebugPrint("[foe] F" + str(j) + " " + dFoe["name"] + " is back")
				dFoe["st"] = SLOT_ALIVE
				dFoe["side"] = dSeen[dFoe["name"]]
			elif dFoe["st"] == SLOT_ALIVE:
				dFoe["st"] = SLOT_DEAD
				DebugPrint("[foe] F" + str(j) + " " + dFoe["name"] + " gone")
		j = j + 1

	# Slot the new arrivals: empty slots first, then RECYCLE dead slots
	# (else a long battle with 9+ lifetime foes goes order-blind on the
	# later waves).
	for sName in dSeen.keys():
		if dSlotted.has_key(sName):
			continue
		iFree = -1
		j = 0
		while j < MAX_FOES:
			if g_lFoes[j]["st"] == SLOT_EMPTY:
				iFree = j
				break
			j = j + 1
		if iFree < 0:
			j = 0
			while j < MAX_FOES:
				if g_lFoes[j]["st"] == SLOT_DEAD:
					iFree = j
					break
				j = j + 1
			if iFree >= 0:
				# A reused slot invalidates any focus pointing at it.
				i = 0
				while i < MAX_FLEET:
					if g_lFleet[i]["focus"] == iFree:
						g_lFleet[i]["focus"] = FOCUS_NONE
					i = i + 1
		if iFree < 0:
			continue
		pShip = GetShipByName(sName)
		g_lFoes[iFree]["st"] = SLOT_ALIVE
		g_lFoes[iFree]["name"] = sName
		g_lFoes[iFree]["side"] = dSeen[sName]
		g_lFoes[iFree]["type"] = ""
		if pShip != None:
			g_lFoes[iFree]["type"] = ShipTypeName(pShip)
		dSlotted[sName] = iFree
		DebugPrint("[foe] F" + str(iFree) + " = " + sName + " side=" + str(dSeen[sName]))


##############################################################################
# Debris janitor
#
# Tonight's evidence (2026-08-04): the scene's damageable-object count rose
# monotonically through a long battle and never fell when ships died, and the
# engine crashed at ~35 objects even WITH the 4GB patch.  If dead hulls linger
# as live objects they cost both frame time and address space, so reap them.
#
# Conservative by construction: only objects that are IsDead() (the explosion
# plays during IsDying(), which we skip), only after REAP_DELAY_TICKS so the
# death sequence and any scoring handlers finish, and NEVER the player.
# SetDeleteMe(1) is the same call QuickBattle.EndSimulation uses on every
# damageable object, so the API itself is stock-proven.
#
# g_dDeadSince maps object NAME -> tick first seen dead: strings and ints
# only, same save-safety contract as the rest of the module.
##############################################################################

REAP_DELAY_TICKS = 4
g_dDeadSince = {}
g_iReaped = 0
g_iPlayerHits = 0		# weapon hits BY the player ON fleet ships (spar taint)
g_sYouHitBy = ""		# Ally mode: name of the last ship to land a hit ON the player
g_iYouHitTick = 0		# ...and the tick it landed


def ReapDebris():
	global g_dDeadSince, g_iReaped
	pPlayer = App.Game_GetCurrentPlayer()
	if pPlayer == None:
		return
	pSet = pPlayer.GetContainingSet()
	if pSet == None:
		return
	sPlayerName = PlayerName()

	dStillDead = {}
	try:
		lObjects = pSet.GetClassObjectList(App.CT_DAMAGEABLE_OBJECT)
	except:
		return

	for pObject in lObjects:
		# NOTE: Python 1.5.2 forbids 'continue' inside a try block (it is
		# legal in the except handler, and legal in modern Python - which
		# is why an offline py_compile does NOT catch it).  So the probe
		# only sets flags, and every 'continue' lives outside the try.
		sName = ""
		bDead = 0
		try:
			if pObject.IsDead():
				bDead = 1
				sName = pObject.GetName()
		except:
			bDead = 0
		if not bDead:
			continue
		if (sName == "") or (sName == sPlayerName):
			continue
		if g_dDeadSince.has_key(sName):
			iSince = g_dDeadSince[sName]
		else:
			iSince = g_iTick
		if (g_iTick - iSince) >= REAP_DELAY_TICKS:
			try:
				pObject.SetDeleteMe(1)
				g_iReaped = g_iReaped + 1
				DebugPrint("[reap] removed dead hull " + sName)
			except:
				dStillDead[sName] = iSince
		else:
			dStillDead[sName] = iSince

	g_dDeadSince = dStillDead


##############################################################################
# Target assertion (per slot, every tick)
##############################################################################

def AssertTargets():
	for i in range(MAX_FLEET):
		dSlot = g_lFleet[i]
		if dSlot["st"] != SLOT_ALIVE:
			continue
		if dSlot["stance"] == "free":
			continue
		if dSlot["man"] == "warp":
			continue
		if (dSlot["focus"] == FOCUS_NONE) and (dSlot["tsub"] == ""):
			continue
		pMine = FleetShip(i)
		if pMine == None:
			continue
		iCode, pTarget = ResolveTarget(i)
		if pTarget == None:
			continue
		try:
			pMine.SetTarget(pTarget)
		except:
			try:
				pMine.SetTarget(pTarget.GetName())
			except:
				pass
		if dSlot["tsub"] != "":
			pSub = GetSubsystem(pTarget, dSlot["tsub"])
			if pSub != None:
				try:
					pMine.SetTargetSubsystem(pSub)
				except:
					pass


##############################################################################
# Telemetry out
##############################################################################

def CleanStr(sText):
	# cfg values must not contain newlines; keep them single-line ASCII.
	sText = string.replace(sText, "\r", " ")
	sText = string.replace(sText, "\n", " ")
	return sText


def BuildState(iStatus):
	# Returns (lInts, lStrs): lists of (key, int) and (key, string).
	lInts = []
	lStrs = []
	lInts.append(("Tick", g_iTick))
	lInts.append(("Status", iStatus))
	lInts.append(("AckSeq", g_iOrders))
	lInts.append(("PlayerHits", g_iPlayerHits))
	lStrs.append(("LastResult", CleanStr(g_sLastResult)))

	pPlayer = App.Game_GetCurrentPlayer()
	lInts.append(("YouAlive", pPlayer != None))
	if pPlayer != None:
		ShipReport("You", pPlayer, lInts)
		# Ally mode: what the player has locked, and who last hit them.
		sYouTgt = TargetNameOf(pPlayer)
		lInts.append(("YouTgtS", FleetIndexOf(sYouTgt)))
		lInts.append(("YouTgtF", FoeIndexOf(sYouTgt)))
		lInts.append(("YouHitByS", FleetIndexOf(g_sYouHitBy)))
		lInts.append(("YouHitByF", FoeIndexOf(g_sYouHitBy)))
		lInts.append(("YouHitTick", g_iYouHitTick))

	iShips = 0
	i = 0
	while i < MAX_FLEET:
		dSlot = g_lFleet[i]
		sP = "S" + str(i)
		if dSlot["st"] != SLOT_EMPTY:
			iShips = iShips + 1
		lInts.append((sP + "St", dSlot["st"]))
		if dSlot["st"] == SLOT_EMPTY:
			i = i + 1
			continue
		# Name/Type never change after spawn - writing them every tick was
		# ~64 wasted SetStringValue calls per second at full fleet.  The
		# cfg mapping has no delete, so a once-written key persists.
		if not dSlot["written"]:
			lStrs.append((sP + "Name", CleanStr(dSlot["name"])))
			lStrs.append((sP + "Type", CleanStr(dSlot["type"])))
			dSlot["written"] = 1
		lInts.append((sP + "Side", dSlot["side"]))
		if dSlot["st"] == SLOT_ALIVE:
			pShip = FleetShip(i)
			if pShip != None:
				ShipReport(sP, pShip, lInts)

				iStance = 0
				for k in range(len(lStanceNames)):
					if lStanceNames[k] == dSlot["stance"]:
						iStance = k
				lInts.append((sP + "Stance", iStance))
				iTSub = 0
				for k in range(len(lTargetNames)):
					if lTargetNames[k] == dSlot["tsub"]:
						iTSub = k
				lInts.append((sP + "TSub", iTSub))
				lInts.append((sP + "Focus", dSlot["focus"]))
				lInts.append((sP + "AbOn", dSlot["abOn"]))
				lInts.append((sP + "AbFires", dSlot["abFires"]))
				# The AI's REAL lock (not the focus order): 1 if on the player.
				sAITgt = TargetNameOf(pShip)
				lInts.append((sP + "OnYou", (sAITgt != "") and (sAITgt == PlayerName())))

				iCode, pTarget = ResolveTarget(i)
				lInts.append((sP + "Tgt", iCode))
				if pTarget != None:
					fRange = Range(pShip, pTarget)
					lInts.append((sP + "Rng", int(fRange)))
					# Closing only against the SAME target as last
					# tick - a focus switch would otherwise produce
					# one tick of garbage delta.
					if (dSlot["lastTgt"] == iCode) and (dSlot["lastRange"] >= 0.0):
						lInts.append((sP + "Closing", int(dSlot["lastRange"] - fRange)))
					else:
						lInts.append((sP + "Closing", 0))
					dSlot["lastRange"] = fRange
					lInts.append((sP + "Brg", int(Bearing(pShip, pTarget))))
				else:
					# Overwrite explicitly: the cfg mapping never
					# deletes keys, so stale values would linger.
					dSlot["lastRange"] = -1.0
					lInts.append((sP + "Rng", -1))
					lInts.append((sP + "Closing", 0))
					lInts.append((sP + "Brg", -1))
				dSlot["lastTgt"] = iCode

				iMan = 0
				if dSlot["man"] == "break":
					iMan = MAN_BREAK
				elif dSlot["man"] == "circle":
					iMan = MAN_CIRCLE
				elif dSlot["man"] == "guard":
					iMan = MAN_GUARD
				elif dSlot["man"] == "warp":
					iMan = MAN_WARP
				lInts.append((sP + "Man", iMan))
				lInts.append((sP + "WB", dSlot["wbPhase"]))
				fSpd, fMax = ShipSpeed(pShip)
				lInts.append((sP + "Spd", int(fSpd)))
				iLeft = 0
				if iMan and (dSlot["manEnd"] > g_iTick):
					iLeft = dSlot["manEnd"] - g_iTick
				lInts.append((sP + "ManLeft", iLeft))
				lInts.append((sP + "ManFail", dSlot["manFail"]))
				lInts.append((sP + "NR", dSlot["needRestore"]))
				lInts.append((sP + "HasAI", HasAI(pShip)))
		i = i + 1
	lInts.append(("NumShips", iShips))

	iFoes = 0
	j = 0
	while j < MAX_FOES:
		dFoe = g_lFoes[j]
		sP = "F" + str(j)
		if dFoe["st"] != SLOT_EMPTY:
			iFoes = iFoes + 1
		lInts.append((sP + "St", dFoe["st"]))
		if dFoe["st"] == SLOT_EMPTY:
			j = j + 1
			continue
		if not dFoe["written"]:
			lStrs.append((sP + "Name", CleanStr(dFoe["name"])))
			lStrs.append((sP + "Type", CleanStr(dFoe["type"])))
			dFoe["written"] = 1
		lInts.append((sP + "Side", dFoe["side"]))
		if dFoe["st"] == SLOT_ALIVE:
			pFoe = FoeShip(j)
			if pFoe != None:
				ShipReport(sP, pFoe, lInts)
				if pPlayer != None:
					lInts.append((sP + "RngYou", int(Range(pFoe, pPlayer))))
				# Range to the nearest live fleet ship.
				fBest = -1.0
				i = 0
				while i < MAX_FLEET:
					if g_lFleet[i]["st"] == SLOT_ALIVE:
						pMine = FleetShip(i)
						if pMine != None:
							fRange = Range(pFoe, pMine)
							if (fRange >= 0.0) and ((fBest < 0.0) or (fRange < fBest)):
								fBest = fRange
					i = i + 1
				if fBest >= 0.0:
					lInts.append((sP + "RngNear", int(fBest)))
		j = j + 1
	lInts.append(("NumFoes", iFoes))

	# Scene-load census: the engine crashes on address-space exhaustion
	# when too many objects accumulate (proven 2026-08-04, C0000005 after
	# the 24-ship siege).  Watch these instead of finding the ceiling by
	# crashing.  Same object-list calls QuickBattle.EndSimulation uses.
	if pPlayer != None:
		try:
			pSet = pPlayer.GetContainingSet()
			if pSet != None:
				iObjs = 0
				for pObject in pSet.GetClassObjectList(App.CT_DAMAGEABLE_OBJECT):
					iObjs = iObjs + 1
				lInts.append(("ObjCount", iObjs))
				iTorps = 0
				for pObject in pSet.GetClassObjectList(App.CT_TORPEDO):
					iTorps = iTorps + 1
				lInts.append(("TorpCount", iTorps))
				lInts.append(("Reaped", g_iReaped))
		except:
			pass

	return (lInts, lStrs)


def WriteState(iStatus):
	# Must never raise: a failure here once killed the whole duel loop.
	global g_bSaveErrShown
	try:
		lInts, lStrs = BuildState(iStatus)
	except:
		lInts = [ ("Tick", g_iTick), ("Status", iStatus), ("BuildErr", 1) ]
		lStrs = []
		DebugPrint("BuildState failed: " + SafeErr())
	try:
		for sKey, iValue in lInts:
			App.g_kConfigMapping.SetIntValue(SEC_STATE, sKey, int(iValue))
		for sKey, sValue in lStrs:
			App.g_kConfigMapping.SetStringValue(SEC_STATE, sKey, sValue)
		App.g_kConfigMapping.SaveConfigFile(STATE_CFG)
	except:
		if not g_bSaveErrShown:
			g_bSaveErrShown = 1
			sMsg = "state save FAILED: " + SafeErr()
			DebugPrint(sMsg)
			Announce("Claude: " + sMsg)


##############################################################################
# Orders in
##############################################################################

def CfgInt(sKey, iDefault):
	try:
		if App.g_kConfigMapping.HasValue(SEC_ORDERS, sKey):
			return App.g_kConfigMapping.GetIntValue(SEC_ORDERS, sKey)
	except:
		pass
	return iDefault


def CfgStr(sKey):
	# Strings are written by the helper as Key=text (SetStringValue form).
	# Fall back to the TGString reader for robustness.
	try:
		if App.g_kConfigMapping.HasValue(SEC_ORDERS, sKey):
			try:
				return str(App.g_kConfigMapping.GetStringValue(SEC_ORDERS, sKey))
			except:
				pass
			try:
				return str(App.g_kConfigMapping.GetTGStringValue(SEC_ORDERS, sKey).GetCString())
			except:
				pass
	except:
		pass
	return ""


def ReadOrderSeq():
	try:
		if App.g_kConfigMapping.LoadConfigFile(ORDER_CFG) == 0:
			return -1
		if not App.g_kConfigMapping.HasValue(SEC_ORDERS, "Seq"):
			return -1
		return App.g_kConfigMapping.GetIntValue(SEC_ORDERS, "Seq")
	except:
		return -1


def GetFreeName(sBase):
	import MissionLib
	global g_iSerial
	sName = sBase
	while MissionLib.GetShip(sName):
		g_iSerial = g_iSerial + 1
		sName = sBase + " " + str(g_iSerial)
	return sName


def AheadPoint(pPlayer, fDistance):
	# The point fDistance ahead of the player's nose: where the enemy side
	# spawns, and what the friendly side is turned to face.
	kLocation = App.TGPoint3()
	kCenter = pPlayer.GetWorldLocation()
	try:
		vForward = pPlayer.GetWorldForwardTG()
		fX = vForward.GetX()
		fY = vForward.GetY()
		fZ = vForward.GetZ()
	except:
		fX, fY, fZ = 0.0, 0.0, 1.0
	kLocation.SetXYZ(kCenter.GetX() + fX * fDistance,
			 kCenter.GetY() + fY * fDistance,
			 kCenter.GetZ() + fZ * fDistance)
	return kLocation


def FaceToward(pShip, kTarget, pRef):
	# 2026-10-24: point pShip's nose at the world point kTarget, keeping
	# roughly pRef's up.  Spawns never set an orientation, so every new
	# ship faced the same world direction: the enemy side (ahead of the
	# player) started with its back to the friendly side (beside the
	# player), and blue won the Damar mirror 3-0 from the first volley.
	# Any failure leaves the ship as it was.
	try:
		kFrom = pShip.GetWorldLocation()
		vFwd = App.TGPoint3()
		vFwd.SetXYZ(kTarget.GetX() - kFrom.GetX(),
			    kTarget.GetY() - kFrom.GetY(),
			    kTarget.GetZ() - kFrom.GetZ())
		if vFwd.Unitize() < 1.0:
			return 0
		lUps = []
		try:
			lUps.append(pRef.GetWorldUpTG())
			lUps.append(pRef.GetWorldForwardTG())
		except:
			pass
		vUp = App.TGPoint3()
		vUp.SetXYZ(0.0, 0.0, 1.0)
		lUps.append(vUp)
		for vCand in lUps:
			fDot = vCand.Dot(vFwd)
			vUp = App.TGPoint3()
			vUp.SetXYZ(vCand.GetX() - vFwd.GetX() * fDot,
				   vCand.GetY() - vFwd.GetY() * fDot,
				   vCand.GetZ() - vFwd.GetZ() * fDot)
			if vUp.Unitize() > 0.05:
				pShip.AlignToVectors(vFwd, vUp)
				pShip.UpdateNodeOnly()
				return 1
	except:
		DebugPrint("face-toward failed: " + SafeErr())
	return 0


def PlaceAhead(pShip, pSet, pPlayer, fDistance):
	# Drop in ahead of the player (turned nose to nose in DoSpawn).
	kLocation = AheadPoint(pPlayer, fDistance)
	iTries = 0
	while (pSet.IsLocationEmptyTG(kLocation, pShip.GetRadius() * 2.0, 1) == 0):
		kLocation.SetXYZ(kLocation.GetX() + 500.0,
				 kLocation.GetY() + 500.0,
				 kLocation.GetZ() + 500.0)
		iTries = iTries + 1
		if iTries > 20:
			break
	pShip.SetTranslate(kLocation)
	pShip.UpdateNodeOnly()


def GetRandom(iRandomMax):
	# -iRandomMax .. +iRandomMax (KM's placement helper)
	if iRandomMax > 0:
		iVal = App.g_kSystemWrapper.GetRandomNumber(iRandomMax)
		if App.g_kSystemWrapper.GetRandomNumber(2) == 1:
			return -iVal
		return iVal
	return 0


def PlaceNearPlayer(pShip, pSet, pPlayer):
	iRandomMax = FRIEND_JITTER
	kLocation = App.TGPoint3()
	kCenter = pPlayer.GetWorldLocation()
	kLocation.SetXYZ(kCenter.GetX() + GetRandom(iRandomMax),
			 kCenter.GetY() + GetRandom(iRandomMax),
			 kCenter.GetZ() + GetRandom(iRandomMax))
	i = 0
	while (pSet.IsLocationEmptyTG(kLocation, pShip.GetRadius() * 2.0, 1) == 0):
		kLocation.SetXYZ(kCenter.GetX() + GetRandom(iRandomMax),
				 kCenter.GetY() + GetRandom(iRandomMax),
				 kCenter.GetZ() + GetRandom(iRandomMax))
		i = i + 1
		if i == 10:
			i = 0
			iRandomMax = iRandomMax + 5000
	pShip.SetTranslate(kLocation)
	pShip.UpdateNodeOnly()


def DoSpawn(iSide, sText):
	# Text = "ShipClass" or "ShipClass,Display Name".
	sText = string.strip(sText)
	if sText == "":
		return "spawn: no ship class"
	iComma = string.find(sText, ",")
	if iComma >= 0:
		sClass = string.strip(sText[:iComma])
		sWantName = string.strip(sText[iComma+1:])
	else:
		sClass = sText
		sWantName = ""
	if sClass == "":
		return "spawn: no ship class"
	if (iSide != 0) and (iSide != 1):
		iSide = 0

	# Free slot?  (Checked before we build anything, so a full fleet costs
	# nothing.  RegisterFleetShip claims the slot for real further down.)
	if FreeFleetSlot() < 0:
		return "spawn: fleet full (" + str(MAX_FLEET) + " live)"

	pPlayer = App.Game_GetCurrentPlayer()
	if (pPlayer == None) or pPlayer.IsDying() or pPlayer.IsDead():
		return "spawn: no player"
	pSet = pPlayer.GetContainingSet()
	if pSet == None:
		return "spawn: no set"

	import loadspacehelper
	try:
		loadspacehelper.PreloadShip(sClass, 1)
	except:
		pass

	if sWantName == "":
		sWantName = sClass
	sDisplayName = GetFreeName(sWantName)

	pShip = None
	try:
		pShip = loadspacehelper.CreateShip(sClass, pSet, sDisplayName, "")
	except:
		pShip = None
	if (pShip == None) or App.IsNull(pShip):
		DebugPrint("could not create " + sClass)
		return "spawn FAILED: " + sClass
	pShip.SetDisplayName(App.TGString(sDisplayName))
	pShip.UpdateNodeOnly()
	if iSide == 1:
		PlaceNearPlayer(pShip, pSet, pPlayer)
		# friendly side: face where the enemy side spawns
		FaceToward(pShip, AheadPoint(pPlayer, g_fSpawnRange), pPlayer)
	else:
		PlaceAhead(pShip, pSet, pPlayer, g_fSpawnRange)
		# enemy side: face the player (and the friendly side beside the player)
		FaceToward(pShip, pPlayer.GetWorldLocation(), pPlayer)
	pProximityManager = pSet.GetProximityManager()
	if pProximityManager:
		pProximityManager.UpdateObject(pShip)

	# Group membership, slot state and AI all happen in one place so the
	# spawn and carrier-launch paths can never drift apart.
	iSlot = RegisterFleetShip(pShip, sClass, iSide)
	if iSlot < 0:
		return "spawn: fleet full (" + str(MAX_FLEET) + " live)"

	if iSide == 1:
		Announce(sDisplayName + " has arrived and is engaging!")
	else:
		Announce(sDisplayName + " has arrived. Claude has the conn.")
	DebugPrint("[spawn] S" + str(iSlot) + " " + sClass + " '" + sDisplayName +
		   "' side=" + str(iSide))
	return "spawned S" + str(iSlot) + " " + sDisplayName


##############################################################################
# Carrier fighter complement (order 15 LAUNCH)
#
# Found 2026-08-05, and it reframes the whole 4-0 series: QuickBattle wires a
# carrier's fighters to a BUTTON ON THE PLAYER'S XO MENU (QuickBattle.py:639-664
# builds "Launch <type> (<n>)" -> SpawnFighter1..7).  Nothing else in the engine
# ever launches them.  A carrier Claude creates through loadspacehelper has no
# bridge, no XO menu and no button, so its complement NEVER launches - Fable's
# six Scorpions sat in the bay for rounds 7, 8 and 9 while the player flew a real
# carrier with four fighters up.  Every "carrier vs carrier" duel was actually
# 1 hull against 5.
#
# This restores the complement the hull is specified to carry - the same
# fighters, from the same stub fields, by the same CreateShip path the player's
# button uses (:880-908).  Not a buff: parity.
##############################################################################

def CarrierWing(sClass):
	# [(fighterClass, quantity), ...] straight off the Bedrock ship stub.
	lOut = []
	sPath = "Custom.Bedrock.Ships." + sClass
	pStub = None
	try:
		# Bare __import__("A.B.C") hands back package A, so ask for a
		# fromlist and keep sys.modules as the belt-and-braces fallback.
		pStub = __import__(sPath, {}, {}, ["FighterType1"])
		if not hasattr(pStub, "FighterType1"):
			pStub = sys.modules[sPath]
	except:
		pStub = None
	if pStub == None:
		return lOut
	i = 1
	while i <= 7:
		sT = "FighterType" + str(i)
		sQ = "FighterQuantity" + str(i)
		if hasattr(pStub, sT) and hasattr(pStub, sQ):
			try:
				sType = getattr(pStub, sT)
				iQty = int(getattr(pStub, sQ))
				if (sType != "") and (sType != None) and (iQty > 0):
					lOut.append((sType, iQty))
			except:
				pass
		i = i + 1
	return lOut


def PlaceNearShip(pNew, pHost, iIndex):
	# Stock fighter placement (QuickBattle.py:888-903), offset from the
	# carrier instead of from the player.
	try:
		fHost = pHost.GetRadius()
		fNew = pNew.GetRadius()
		if fNew < 0.25:
			fNew = 0.25
		kLoc = pHost.GetWorldLocation()
		kX = pHost.GetWorldRightTG()
		kZ = pHost.GetWorldUpTG()
		kX.Scale(fHost + (fNew * iIndex * 3.0))
		kZ.Scale((fNew * iIndex * 3.0) - (fHost / 2.0))
		kLoc.Subtract(kX)
		kLoc.Add(kZ)
		pNew.SetTranslate(kLoc)
		pNew.AlignToVectors(pHost.GetWorldForwardTG(), pHost.GetWorldUpTG())
	except:
		DebugPrint("fighter placement failed: " + SafeErr())


def FreeFleetSlot():
	# Empty slots first, then RECYCLE a dead one.
	#
	# Without the recycle a long attritional battle starves itself: a
	# destroyed ship keeps its slot as SLOT_DEAD forever, so after
	# MAX_FLEET lifetime spawns every further spawn fails "fleet full"
	# even with only three ships actually alive.  That happened TWICE in
	# the 2026-08-05 Borg fight and both times needed a full link reset
	# (the player re-pressing the button) mid-battle to clear it.  UpdateFoes
	# has recycled dead FOE slots since v3 for exactly this reason; the
	# fleet side just never got the same treatment.
	i = 0
	while i < MAX_FLEET:
		if g_lFleet[i]["st"] == SLOT_EMPTY:
			return i
		i = i + 1
	i = 0
	while i < MAX_FLEET:
		if g_lFleet[i]["st"] == SLOT_DEAD:
			DebugPrint("[slot] recycling S" + str(i) + " (was " +
				   g_lFleet[i]["name"] + ")")
			return i
		i = i + 1
	return -1


def RegisterFleetShip(pShip, sClass, iSide):
	# Slot a freshly created ship, put it in its side's group (group
	# membership IS hostility), and hand it the command AI.
	iSlot = FreeFleetSlot()
	if iSlot < 0:
		return -1
	OwnGroup(iSide).AddName(pShip.GetName())
	# Rebuild the slot from the template rather than assigning fields one
	# by one.  A recycled slot carries the DEAD ship's state, and missing
	# even one field leaks it into the new ship - "written" is the nasty
	# one: leave it set and BuildState never re-emits Name/Type, so the
	# console would keep showing the corpse's name and class forever.
	g_lFleet[iSlot] = NewFleetSlot()
	dSlot = g_lFleet[iSlot]
	dSlot["st"] = SLOT_ALIVE
	dSlot["name"] = pShip.GetName()
	dSlot["type"] = sClass
	dSlot["side"] = iSide
	if not ApplyStance(iSlot, "balanced"):
		DebugPrint("S" + str(iSlot) + " stance failed on register - stock AI")
		ApplyStockAI(iSlot)
	return iSlot


def DoLaunch(iSlot, iMax):
	dSlot = g_lFleet[iSlot]
	pHost = FleetShip(iSlot)
	if pHost == None:
		return "no ship"
	lWing = CarrierWing(dSlot["type"])
	if not lWing:
		return dSlot["type"] + " carries no fighters"
	pSet = pHost.GetContainingSet()
	if pSet == None:
		return "no set"

	import loadspacehelper
	iLaunched = 0
	iIndex = 1
	bFull = 0
	for sType, iQty in lWing:
		try:
			loadspacehelper.PreloadShip(sType, 1)
		except:
			pass
		j = 0
		while j < iQty:
			if (iMax > 0) and (iLaunched >= iMax):
				j = iQty
			elif FreeFleetSlot() < 0:
				bFull = 1
				j = iQty
			else:
				sName = GetFreeName(sType)
				pF = None
				try:
					pF = loadspacehelper.CreateShip(sType, pSet, sName, "")
				except:
					pF = None
					DebugPrint("fighter create failed: " + SafeErr())
				if (pF != None) and (not App.IsNull(pF)):
					try:
						pF.SetDisplayName(App.TGString(sName))
						pF.UpdateNodeOnly()
					except:
						pass
					PlaceNearShip(pF, pHost, iIndex)
					pProx = pSet.GetProximityManager()
					if pProx:
						try:
							pProx.UpdateObject(pF)
						except:
							pass
					if RegisterFleetShip(pF, sType, dSlot["side"]) >= 0:
						iLaunched = iLaunched + 1
						iIndex = iIndex + 1
				j = j + 1
	if iLaunched == 0:
		return "no fighters launched"
	sOut = "launched " + str(iLaunched) + " fighters from " + dSlot["name"]
	if bFull:
		sOut = sOut + " (fleet slots full)"
	Announce(dSlot["name"] + " launches fighters!")
	DebugPrint("[launch] S" + str(iSlot) + " " + sOut)
	return sOut


def DoTarget(iSlot, sArg):
	dSlot = g_lFleet[iSlot]
	sArg = string.lower(string.strip(sArg))
	if (sArg == "") or (sArg == "none"):
		dSlot["tsub"] = ""
		ApplyStance(iSlot, dSlot["stance"])
		return "targeting released"
	iCode, pTarget = ResolveTarget(iSlot)
	if pTarget == None:
		return "no target ship"
	if GetSubsystem(pTarget, sArg) == None:
		return "target has no " + sArg
	# ALWAYS rebuild: the TargetSubsystems list is baked into the attack
	# AI at build time, so switching weapons->engines without a rebuild
	# keeps aiming at weapons (v2 latent bug - it only rebuilt when the
	# previous target was 'none').
	dSlot["tsub"] = sArg
	ApplyStance(iSlot, dSlot["stance"])
	return "targeting " + sArg


def DoPower(iSlot, sArg):
	sArg = string.lower(string.strip(sArg))
	pShip = FleetShip(iSlot)
	if pShip == None:
		return "no ship"

	dProfiles = {
		"weapons":  (("weapons", 1.25), ("torps", 1.25), ("shields", 0.85), ("engines", 0.8)),
		"shields":  (("shields", 1.25), ("weapons", 0.85), ("engines", 0.85)),
		"engines":  (("engines", 1.25), ("warp", 1.1), ("weapons", 0.85), ("shields", 0.9)),
		"balanced": (("weapons", 1.0), ("torps", 1.0), ("shields", 1.0), ("engines", 1.0), ("warp", 1.0)),
	}
	if not dProfiles.has_key(sArg):
		return "unknown power profile: " + sArg

	iDone = 0
	for sSystem, fLevel in dProfiles[sArg]:
		pSub = GetSubsystem(pShip, sSystem)
		if pSub == None:
			continue
		try:
			pSub.SetPowerPercentageWanted(fLevel)
			iDone = iDone + 1
		except:
			pass
	return "power to " + sArg + " (" + str(iDone) + " systems)"


def DoRepair(iSlot):
	# Make sure the ship is actually repairing, and give the repair bay
	# power priority.
	#
	# NOTE on what is and is not possible here (researched 2026-08-05):
	# the drag-and-drop repair PRIORITY queue the player drives from engineering
	# is player-UI only - App.EngRepairPane_GetRepairPane() is a singleton
	# bound to the player's own screen (E2M0.py:2732, 4064), and no
	# per-ship priority API is exposed to script.  So Claude cannot order
	# "fix shields before engines" the way the player can; his repair edge is
	# partly structural.  What IS available is keeping the bay enabled and
	# powered, which is the honest equivalent.  Deliberately NOT touching
	# SetMaxRepairPoints/SetNumRepairTeams: those are hull stats, they come
	# off a shared class property, and buffing them mid-duel would be
	# cheating rather than playing.
	pShip = FleetShip(iSlot)
	if pShip == None:
		return "no ship"
	pRep = None
	try:
		pRep = pShip.GetRepairSubsystem()
	except:
		pRep = None
	if pRep == None:
		return "no repair subsystem"
	sOut = "repair bay"
	try:
		pRep.SetEnabled(1)
		sOut = sOut + " enabled"
	except:
		pass
	try:
		pRep.SetPowerPercentageWanted(1.25)
		sOut = sOut + ", power priority"
	except:
		pass
	iCond = Pct(Health(pRep))
	sEng = QueueEngineRepairs(pShip, pRep)
	return sOut + " (bay at " + str(iCond) + "%)" + sEng


def ImpulseKids(pShip):
	lKids = []
	try:
		pImp = pShip.GetImpulseEngineSubsystem()
		iKids = pImp.GetNumChildSubsystems()
	except:
		return lKids
	i = 0
	while i < iKids:
		try:
			pChild = pImp.GetChildSubsystem(i)
			if pChild != None:
				lKids.append(pChild)
		except:
			pass
		i = i + 1
	return lKids


def EngineRepairCount(pShip, pRep):
	# Impulse engines the repair bay is working on right now (-1 unreadable).
	if pRep == None:
		return -1
	iOn = 0
	for pChild in ImpulseKids(pShip):
		try:
			if pRep.IsBeingRepaired(pChild):
				iOn = iOn + 1
		except:
			return -1
	return iOn


def QueueEngineRepairs(pShip, pRep):
	# EXPERIMENT (10-29): her impulse engines sat at 0% for
	# 150 ticks while the bay patched the hull 33 -> 41.  The 08-05 note
	# says the repair PRIORITY pane is player-only; App also exposes
	# RepairSubsystem.AddToRepairList, untested until now.  Put every
	# damaged impulse engine the bay is not already on onto its list, and
	# report what it says - EngRep in telemetry shows whether it took.
	iQueued = 0
	iFail = 0
	lKids = ImpulseKids(pShip)
	for pChild in lKids:
		# NB: no `continue` inside a try - a SyntaxError on 1.5.2
		bWhole = 0
		try:
			bWhole = (Health(pChild) >= 1.0)
		except:
			bWhole = 0
		bOn = 0
		try:
			bOn = pRep.IsBeingRepaired(pChild)
		except:
			bOn = 0
		if (not bWhole) and (not bOn):
			try:
				pRep.AddToRepairList(pChild)
				iQueued = iQueued + 1
			except:
				iFail = iFail + 1
	iOn = EngineRepairCount(pShip, pRep)
	sOut = ""
	if lKids:
		sOut = (" - engines: queued " + str(iQueued) + ", failed " +
			str(iFail) + ", being repaired " + str(iOn) + " of " +
			str(len(lKids)))
		DebugPrint("[repair]" + sOut)
	return sOut


def DoCloak(iSlot, bOn):
	pShip = FleetShip(iSlot)
	if pShip == None:
		return "no ship"
	try:
		pCloak = pShip.GetCloakingSubsystem()
	except:
		pCloak = None
	if pCloak == None:
		return "no cloaking device"
	if bOn:
		try:
			pCloak.StartCloaking()
		except:
			return "cloak failed"
		# Cloaked guns cannot fire, so weapons power is dead weight -
		# fold it into shields and the repair bay for the duration.  This
		# is the same trick the player runs from his engineering macros, and it
		# is why he comes out of every cloak healthier than he went in.
		sExtra = ""
		try:
			DoPower(iSlot, "shields")
			DoRepair(iSlot)
			sExtra = " (power to shields + repair)"
		except:
			sExtra = ""
		return "cloaking" + sExtra
	# StopCloaking is the real decloak call (E3M4.py:765); the others are
	# guesses kept as fallbacks for other engine builds.
	for sMethod in ("StopCloaking", "StartDecloaking", "Decloak"):
		try:
			apply(getattr(pCloak, sMethod), ())
			return "decloaking"
		except:
			pass
	return "decloak failed"


def DoFocus(iSlot, iArg):
	# Every path that changes dSlot["focus"] must go through RefocusAI:
	# the focus lives in the AI's target group, not in a pointer we can
	# poke each tick.  See FocusGroup for why.
	dSlot = g_lFleet[iSlot]
	if iArg == FOCUS_NONE:
		dSlot["focus"] = FOCUS_NONE
		RefocusAI(iSlot)
		return "focus released"
	if iArg == FOCUS_SPAR:
		dSlot["focus"] = FOCUS_SPAR
		if not RefocusAI(iSlot):
			return "spar focus - will ignore the player (applies when the manoeuvre ends)"
		return "spar focus - ignoring the player"
	if iArg == FOCUS_PLAYER:
		if dSlot["side"] == 1:
			return "friendly ship will not focus the player"
		dSlot["focus"] = FOCUS_PLAYER
		if not RefocusAI(iSlot):
			return "focusing the player (applies when the manoeuvre ends)"
		return "focusing the player"
	if (iArg >= 0) and (iArg < MAX_FOES):
		if g_lFoes[iArg]["st"] != SLOT_ALIVE:
			return "foe F" + str(iArg) + " not alive"
		if g_lFoes[iArg]["side"] == dSlot["side"]:
			return "F" + str(iArg) + " is on our side"
		dSlot["focus"] = iArg
		sWho = "F" + str(iArg) + " " + g_lFoes[iArg]["name"]
		if not RefocusAI(iSlot):
			return "focusing " + sWho + " (applies when the manoeuvre ends)"
		return "focusing " + sWho
	if (iArg >= SPAR_TGT_BASE) and (iArg < SPAR_TGT_BASE + MAX_FLEET):
		# Ally mode: DUELAI's reds are FLEET ships, not foes, so a wingman
		# needs to be locked to one by fleet slot.  Same code the telemetry
		# already uses for spar targets (SPAR_TGT_BASE + slot).
		j = iArg - SPAR_TGT_BASE
		if (j == iSlot) or (g_lFleet[j]["st"] != SLOT_ALIVE):
			return "fleet S" + str(j) + " not alive"
		if g_lFleet[j]["side"] == dSlot["side"]:
			return "S" + str(j) + " is on our side"
		dSlot["focus"] = iArg
		sWho = "S" + str(j) + " " + g_lFleet[j]["name"]
		if not RefocusAI(iSlot):
			return "focusing " + sWho + " (applies when the manoeuvre ends)"
		return "focusing " + sWho
	return "bad focus code"


def DoTaunt(iSlot, sText):
	sText = string.strip(sText)
	if sText == "":
		return "empty taunt"
	sSpeaker = "Claude"
	if (iSlot >= 0) and (iSlot < MAX_FLEET) and (g_lFleet[iSlot]["st"] == SLOT_ALIVE):
		sSpeaker = g_lFleet[iSlot]["name"]
	Announce(sSpeaker + ": " + sText)
	return "taunted"


def LiveSlots():
	lOut = []
	i = 0
	while i < MAX_FLEET:
		if g_lFleet[i]["st"] == SLOT_ALIVE:
			lOut.append(i)
		i = i + 1
	return lOut


def FleetIndexOf(sName):
	# Fleet slot index of a live ship called sName, -1 if none.
	if sName == "":
		return -1
	i = 0
	while i < MAX_FLEET:
		if (g_lFleet[i]["st"] == SLOT_ALIVE) and (g_lFleet[i]["name"] == sName):
			return i
		i = i + 1
	return -1


def FoeIndexOf(sName):
	if sName == "":
		return -1
	j = 0
	while j < MAX_FOES:
		if (g_lFoes[j]["st"] == SLOT_ALIVE) and (g_lFoes[j]["name"] == sName):
			return j
		j = j + 1
	return -1


def TargetNameOf(pShip):
	# Name of what this ship is actually locked onto (the AI's real pick,
	# not our focus order), "" for nothing.
	try:
		pTarget = pShip.GetTarget()
		if pTarget != None:
			return pTarget.GetName()
	except:
		pass
	return ""


def PlayerHitHandler(pObject, pEvent):
	# Spar taint detection: count weapon hits the PLAYER lands on any
	# fleet-registered ship.  DUELAI reads the running total (PlayerHits in
	# the state file) and quarantines sparring rounds the player meddled in.
	global g_iPlayerHits, g_sYouHitBy, g_iYouHitTick
	if g_bActive:
		sFirer = ""
		sTarget = ""
		try:
			pFirer = App.ShipClass_Cast(pEvent.GetFiringObject())
			if pFirer != None:
				sFirer = pFirer.GetName()
			pTarget = pEvent.GetDestination()
			if pTarget != None:
				sTarget = pTarget.GetName()
		except:
			sFirer = ""
		if (sFirer != "") and (sTarget != "") and (sTarget == PlayerName()) and (sFirer != sTarget):
			g_sYouHitBy = sFirer
			g_iYouHitTick = g_iTick
		if (sFirer != "") and (sFirer == PlayerName()) and (sTarget != ""):
			i = 0
			while i < MAX_FLEET:
				if (g_lFleet[i]["st"] == SLOT_ALIVE) and (g_lFleet[i]["name"] == sTarget):
					g_iPlayerHits = g_iPlayerHits + 1
					i = MAX_FLEET
				i = i + 1
	pObject.CallNextHandler(pEvent)


def DoReapFleet():
	# Sparring board-clear: remove every LIVE fleet-registered ship (both
	# sides) from the mission.  END/StandDown never despawns survivors and
	# ReapDebris only collects the dead, so round 2 of a spar set was
	# fought against round 1's victors as well.  The player is never in
	# the fleet table, so this cannot touch them.
	iN = 0
	i = 0
	while i < MAX_FLEET:
		dSlot = g_lFleet[i]
		if dSlot["st"] == SLOT_ALIVE:
			pShip = GetShipByName(dSlot["name"])
			if pShip != None:
				try:
					pShip.SetDeleteMe(1)
					iN = iN + 1
				except:
					pass
			dSlot["st"] = SLOT_DEAD
		i = i + 1
	DebugPrint("[reapfleet] removed " + str(iN) + " live hulls")
	return "fleet reaped: " + str(iN)


def DoSetSpawnRange(iArg):
	# Order 17.  Global, not per-slot: it only affects the NEXT spawn.
	global g_fSpawnRange
	f = float(iArg)
	if f < 150.0:
		f = 150.0
	if f > 20000.0:
		f = 20000.0
	g_fSpawnRange = f
	DebugPrint("[spawnrange] engagement distance set to " + str(int(f)))
	return "spawn range " + str(int(f))


def RunOrder(iCmd, iArg, iShip, sText):
	# Returns a result string.  iShip -1 = broadcast to all live slots
	# (except the inherently global orders).
	if iCmd == 10:
		return DoSpawn(iArg, sText)
	if iCmd == 16:
		return DoReapFleet()
	if iCmd == 17:
		return DoSetSpawnRange(iArg)
	if iCmd == 5 and iShip < 0:
		return DoTaunt(-1, sText)

	if iShip < 0:
		lSlots = LiveSlots()
		if len(lSlots) == 0:
			return "no live ships"
		lResults = []
		for i in lSlots:
			lResults.append("S" + str(i) + ":" + RunOrderOne(iCmd, iArg, i, sText))
		return string.join(lResults, " ")
	if iShip >= MAX_FLEET:
		return "bad ship slot"
	if g_lFleet[iShip]["st"] != SLOT_ALIVE:
		return "S" + str(iShip) + " not alive"
	return RunOrderOne(iCmd, iArg, iShip, sText)


def RunOrderOne(iCmd, iArg, iSlot, sText):
	if iCmd == 18:
		if iArg == 0:
			if g_lFleet[iSlot]["man"] == "warp":
				EndManeuver(iSlot, "warp cancelled")
				return "warp break cancelled"
			return "no warp break running"
		return StartWarpBreak(iSlot, ClampSeconds(iArg, WARP_MEND_DEF, WARP_MEND_MIN, WARP_MEND_MAX))
	if g_lFleet[iSlot]["man"] == "warp":
		# Any AI rebuild would abandon the run (or strand the ship mid-mend
		# far from the fight), so these wait for the warp break to end.
		if iCmd == 1:
			if (iArg >= 1) and (iArg < len(lStanceNames) - 1):
				g_lFleet[iSlot]["stance"] = lStanceNames[iArg]
				return "stance " + lStanceNames[iArg] + " queued (warp break)"
			return "bad stance code"
		if iCmd == 2:
			if (iArg >= 0) and (iArg < len(lTargetNames)):
				sSub = lTargetNames[iArg]
				if sSub == "none":
					sSub = ""
				g_lFleet[iSlot]["tsub"] = sSub
				return "target queued (warp break)"
			return "bad target code"
		if (iCmd == 7) or (iCmd == 8) or (iCmd == 9):
			if iArg == 0:
				EndManeuver(iSlot, "warp cancelled")
				return "warp break cancelled"
			return "refused: warp break running"
		if iCmd == 11:
			return "focus refused: warp break running"
	if iCmd == 1:
		if (iArg >= 1) and (iArg < len(lStanceNames) - 1):
			# gunboat: the close band rides in the text, in metres.  Only a bare
			# number counts - the cfg mapping never deletes keys, so an order
			# sent with no text reads whatever an older batch left in that slot.
			sNum = string.strip(sText)
			if (sNum != "") and (string.find(sNum, ",") < 0):
				try:
					g_lFleet[iSlot]["closeRange"] = float(sNum)
				except:
					pass
			if ApplyStance(iSlot, lStanceNames[iArg]):
				return "stance " + g_lFleet[iSlot]["stance"]
			return "stance failed"
		return "bad stance code"
	if iCmd == 2:
		if (iArg >= 0) and (iArg < len(lTargetNames)):
			return DoTarget(iSlot, lTargetNames[iArg])
		return "bad target code"
	if iCmd == 3:
		if (iArg >= 1) and (iArg < len(lPowerNames)):
			return DoPower(iSlot, lPowerNames[iArg])
		return "bad power code"
	if iCmd == 4:
		return DoCloak(iSlot, iArg)
	if iCmd == 5:
		return DoTaunt(iSlot, sText)
	if (iCmd == 7) or (iCmd == 8) or (iCmd == 9):
		if iArg == 0:
			if g_lFleet[iSlot]["man"] != "":
				EndManeuver(iSlot, "cancelled")
				return "manoeuvre cancelled"
			return "no manoeuvre running"
		if iCmd == 7:
			return StartManeuver(iSlot, MAN_BREAK,
				ClampSeconds(iArg, BREAK_DEF_SEC, BREAK_MIN_SEC, BREAK_MAX_SEC))
		if iCmd == 9:
			return StartManeuver(iSlot, MAN_GUARD,
				ClampSeconds(iArg, CIRCLE_DEF_SEC, CIRCLE_MIN_SEC, CIRCLE_MAX_SEC))
		return StartManeuver(iSlot, MAN_CIRCLE,
			ClampSeconds(iArg, CIRCLE_DEF_SEC, CIRCLE_MIN_SEC, CIRCLE_MAX_SEC))
	if iCmd == 11:
		if iArg == FOCUS_SPAR:
			g_lFleet[iSlot]["spar"] = 1
		return DoFocus(iSlot, iArg)
	if iCmd == 12:
		if ApplyStockAI(iSlot):
			return "freed to stock AI"
		return "free failed"
	if iCmd == 13:
		return DoAutoBreak(iSlot, iArg, sText)
	if iCmd == 14:
		return DoRepair(iSlot)
	if iCmd == 15:
		return DoLaunch(iSlot, iArg)
	return "unknown order code"


def ProcessOrders():
	global g_iOrders, g_sLastResult
	iSeq = ReadOrderSeq()
	if iSeq <= g_iOrders:
		return
	g_iOrders = iSeq

	iNum = CfgInt("Num", 0)
	if iNum < 1:
		# v2-style single order without Num - honour it anyway.
		iNum = 1
	if iNum > 16:
		iNum = 16

	lResults = []
	j = 0
	while j < iNum:
		sJ = str(j)
		iCmd = CfgInt("Cmd" + sJ, 0)
		iArg = CfgInt("Arg" + sJ, 0)
		iShip = CfgInt("Ship" + sJ, -1)
		sText = CfgStr("Text" + sJ)
		if iCmd == 0:
			j = j + 1
			continue
		if iCmd == 6:
			# END is handled here, not in RunOrder: the result must be
			# in g_sLastResult BEFORE StandDown writes the final state,
			# and no later order in the batch may run against a dead
			# link.
			lResults.append("link ended")
			g_sLastResult = TrimResult(lResults)
			DebugPrint("order " + str(iSeq) + "." + sJ + " -> link ended")
			StandDown("Claude has closed the command link.", ST_OVER)
			return
		try:
			sResult = RunOrder(iCmd, iArg, iShip, sText)
		except:
			sResult = "order failed: " + SafeErr()
		DebugPrint("order " + str(iSeq) + "." + sJ + " (" + str(iCmd) + "," +
			   str(iArg) + ",ship=" + str(iShip) + ") -> " + sResult)
		lResults.append(sResult)
		j = j + 1
	g_sLastResult = TrimResult(lResults)


def TrimResult(lResults):
	sOut = string.join(lResults, "; ")
	if len(sOut) > 240:
		sOut = sOut[:240]
	return sOut


##############################################################################
# Link lifecycle
##############################################################################

def AddTimer(eType, fSeconds):
	pCurGame = App.Game_GetCurrentGame()
	if pCurGame == None:
		return
	pEvent = App.TGEvent_Create()
	pEvent.SetEventType(eType)
	pEvent.SetDestination(pCurGame)
	pTimer = App.TGTimer_Create()
	pTimer.SetTimerStart(App.g_kUtopiaModule.GetGameTime() + fSeconds)
	pTimer.SetDelay(0)
	pTimer.SetDuration(0)
	pTimer.SetEvent(pEvent)
	App.g_kTimerManager.AddTimer(pTimer)


def StandDown(sWhy, iStatus):
	global g_bActive
	if g_bActive:
		# End manoeuvres FIRST: with the loop dead nothing else would
		# ever restore those ships' attack AIs.
		i = 0
		while i < MAX_FLEET:
			if (g_lFleet[i]["st"] == SLOT_ALIVE) and (g_lFleet[i]["man"] != ""):
				try:
					EndManeuver(i, "stand down")
				except:
					pass
			i = i + 1
		g_bActive = 0
		DebugPrint("[end] " + sWhy)
		Announce(sWhy)
	WriteState(iStatus)


def StartLink():
	global g_bActive, g_iOrders, g_iTick, g_sLastResult, g_dEvicted

	pPlayer = App.Game_GetCurrentPlayer()
	if (pPlayer == None) or pPlayer.IsDying() or pPlayer.IsDead():
		return

	if g_bActive:
		Announce("Claude is already on the line, Captain.")
		return

	ResetSlots()
	# QuickBattle's GenerateShips re-populates both groups on restart, so
	# last battle's evictions mean nothing to this one; clear the log so a
	# fresh rock gets reported again.
	g_dEvicted = {}
	g_iTick = 0
	g_sLastResult = ""
	g_bActive = 1

	# Never replay orders from a previous battle: skip to whatever Seq is
	# already in the orders file.
	iSeq = ReadOrderSeq()
	if iSeq > 0:
		g_iOrders = iSeq
	else:
		g_iOrders = 0

	# Arm the tick loop FIRST; nothing below may prevent it.
	AddTimer(App.ET_CLAUDE_TICK, TICK_TIME)
	DebugPrint("[start] command link up, orders at seq " + str(g_iOrders))
	Announce("Claude command link established. Awaiting orders.")
	WriteState(ST_ACTIVE)


def TickHandler(pObject, pEvent):
	global g_iTick
	if not g_bActive:
		pObject.CallNextHandler(pEvent)
		return

	g_iTick = g_iTick + 1

	# A missing player is normally death - but TransferCommand deletes the
	# old hull and creates a new one, so there is a legitimate window of a
	# tick or two with no player.  Require it to persist before standing
	# down, or a ship transfer would kill the link every time.
	global g_iNoPlayer
	pPlayer = App.Game_GetCurrentPlayer()
	if (pPlayer == None) or pPlayer.IsDead() or pPlayer.IsDying():
		g_iNoPlayer = g_iNoPlayer + 1
		if g_iNoPlayer >= NO_PLAYER_GRACE:
			StandDown("Claude's link drops - the Captain is gone.", ST_PLAYER_DEAD)
		pObject.CallNextHandler(pEvent)
		if g_iNoPlayer < NO_PLAYER_GRACE:
			AddTimer(App.ET_CLAUDE_TICK, TICK_TIME)
		return
	g_iNoPlayer = 0

	# Death sweep: a destroyed fleet ship keeps its slot as SLOT_DEAD.
	i = 0
	while i < MAX_FLEET:
		dSlot = g_lFleet[i]
		if dSlot["st"] == SLOT_ALIVE:
			pShip = GetShipByName(dSlot["name"])
			if (pShip == None) or pShip.IsDead() or pShip.IsDying():
				dSlot["st"] = SLOT_DEAD
				dSlot["man"] = ""
				dSlot["manEnd"] = 0
				dSlot["needRestore"] = 0
				DebugPrint("[dead] S" + str(i) + " " + dSlot["name"])
				Announce(dSlot["name"] + " has been destroyed.")
		i = i + 1

	try:
		EvictFiller()
	except:
		DebugPrint("filler eviction failed: " + SafeErr())
	try:
		UpdateFoes()
	except:
		DebugPrint("foe update failed: " + SafeErr())
	try:
		ReapDebris()
	except:
		DebugPrint("debris reap failed: " + SafeErr())
	try:
		ProcessOrders()
	except:
		DebugPrint("order processing failed: " + SafeErr())
	if not g_bActive:
		# An END order stood us down mid-tick; don't overwrite its
		# status or re-arm the loop.
		pObject.CallNextHandler(pEvent)
		return
	try:
		ValidateFocus()
	except:
		DebugPrint("focus validation failed: " + SafeErr())
	try:
		AssertTargets()
	except:
		pass
	# Defensive reflex BEFORE the watchdog: if this tick is the one where a
	# facing gives way, the break must start now, not next second.
	try:
		CheckAutoBreak()
	except:
		DebugPrint("autobreak sweep failed: " + SafeErr())
	i = 0
	while i < MAX_FLEET:
		if g_lFleet[i]["st"] == SLOT_ALIVE:
			try:
				ManeuverWatchdog(i)
			except:
				DebugPrint("watchdog S" + str(i) + " raised: " + SafeErr())
				try:
					EndManeuver(i, "watchdog error")
				except:
					pass
		i = i + 1
	WriteState(ST_ACTIVE)

	AddTimer(App.ET_CLAUDE_TICK, TICK_TIME)
	pObject.CallNextHandler(pEvent)


##############################################################################
# BORG DRIVE - player-ship collision override (for the player, not for Claude)
#
# A Cube is enormous and turns like a cathedral; in a crowded fight it spends
# its time being shoved off course or wedged against wrecks and asteroids.
# This toggle takes the player's hull out of the collision system entirely so
# it simply drives through whatever is in the way - which is also exactly how
# a Cube ought to feel.
#
# Both calls are stock: ProximityManager_SetPlayerCollisionsEnabled is what
# the Options screen's "Collisions" checkbox drives (mainmenu.py:5577, 6783),
# and ShipClass.SetCollisionsOn is used all over the campaign (E2M0.py:726).
# Steering and throttle stay entirely in the player's hands - this only stops the
# world from stopping him.
##############################################################################

def ApplyBorgDrive():
	bCollide = not g_bBorgDrive
	try:
		App.ProximityManager_SetPlayerCollisionsEnabled(bCollide)
		App.ProximityManager_SetMultiplayerPlayerCollisionsEnabled(bCollide)
	except:
		DebugPrint("player collision toggle failed: " + SafeErr())
	pPlayer = App.Game_GetCurrentPlayer()
	if pPlayer != None:
		try:
			pPlayer.SetCollisionsOn(bCollide)
		except:
			pass


def BorgDriveHandler(pObject, pEvent):
	global g_bBorgDrive
	g_bBorgDrive = not g_bBorgDrive
	ApplyBorgDrive()
	if g_bBorgDrive:
		Announce("Borg drive engaged. Obstacles are irrelevant.")
		DebugPrint("[drive] player collisions OFF")
	else:
		Announce("Borg drive disengaged. Collisions restored.")
		DebugPrint("[drive] player collisions ON")


def DuelHandler(pObject, pEvent):
	if not InQuickBattle():
		return
	try:
		StartLink()
	except:
		DebugPrint("StartLink failed: " + SafeErr())


def EnsureButton():
	global g_idButton
	pButton = App.STButton_Cast(App.TGObject_GetTGObjectPtr(g_idButton))
	if pButton:
		return
	pMenu = GetTacticalMenu()
	if not pMenu:
		DebugPrint("ERROR - could not find Tactical menu")
		return
	import MissionLib
	import ftb.GUIUtils
	pButton = ftb.GUIUtils.CreateIntButton("Duel Claude", App.ET_INPUT_CLAUDE_DUEL, MissionLib.GetMission(), 0)
	pMenu.AddChild(pButton)
	g_idButton = pButton.GetObjID()
	DebugPrint("tactical button created")
	EnsureDriveButton()


def EnsureDriveButton():
	global g_idDriveButton
	if App.STButton_Cast(App.TGObject_GetTGObjectPtr(g_idDriveButton)):
		return
	pMenu = GetHelmMenu()
	if not pMenu:
		return
	import MissionLib
	import ftb.GUIUtils
	pButton = ftb.GUIUtils.CreateIntButton("Borg Drive", App.ET_INPUT_BORG_DRIVE,
					       MissionLib.GetMission(), 0)
	pMenu.AddChild(pButton)
	g_idDriveButton = pButton.GetObjID()
	DebugPrint("borg drive button created")


def MissionStartHandler(pObject, pEvent):
	global g_bActive, g_iOrders, g_iTick, g_idButton, g_bSaveErrShown
	global g_iSerial, g_sLastResult, g_dDeadSince, g_iReaped, g_iPlayerHits, g_sYouHitBy, g_iYouHitTick
	global g_idDriveButton, g_bBorgDrive, g_iNoPlayer
	g_bActive = 0
	g_iOrders = 0
	g_iTick = 0
	g_bSaveErrShown = 0
	g_idButton = App.NULL_ID
	# New mission, new buttons; drive mode starts off and the engine's own
	# collision setting is left exactly as the player configured it.
	g_idDriveButton = App.NULL_ID
	g_bBorgDrive = 0
	g_iNoPlayer = 0
	g_iSerial = 1
	g_sLastResult = ""
	g_dDeadSince = {}
	g_iReaped = 0
	g_iPlayerHits = 0
	g_sYouHitBy = ""
	g_iYouHitTick = 0
	ResetSlots()

	lEventHandlerMap = (
		(App.ET_INPUT_CLAUDE_DUEL, __name__ + ".DuelHandler"),
		(App.ET_INPUT_BORG_DRIVE, __name__ + ".BorgDriveHandler"),
		(App.ET_CLAUDE_TICK, __name__ + ".TickHandler"),
		(App.ET_CLAUDE_MANEUVER_END, __name__ + ".ManeuverEndHandler"),
		(App.ET_WEAPON_HIT, __name__ + ".PlayerHitHandler"),
	)
	pCurGame = App.Game_GetCurrentGame()
	for eType, sFunc in lEventHandlerMap:
		App.g_kEventManager.RemoveBroadcastHandler(eType, pCurGame, sFunc)
		App.g_kEventManager.AddBroadcastPythonFuncHandler(eType, pCurGame, sFunc)

	# Idempotent (guarded by AvoidObstacles.claudeTuned) - here as well as at
	# import so a mid-session reload still gets it.
	PatchStockAvoider()

	if InQuickBattle():
		try:
			EnsureButton()
		except:
			DebugPrint("could not create tactical button")

	# Heartbeat through the engine config channel: proves the bridge works
	# before the link ever starts.
	WriteState(ST_IDLE)
	DebugPrint("mission start heartbeat written via " + STATE_CFG)
	pObject.CallNextHandler(pEvent)


pGame = App.Game_GetCurrentGame()
if pGame:
	App.g_kEventManager.RemoveBroadcastHandler(
		App.ET_MISSION_START,
		pGame,
		__name__ + ".MissionStartHandler"
	)
	App.g_kEventManager.AddBroadcastPythonFuncHandler(
		App.ET_MISSION_START,
		pGame,
		__name__ + ".MissionStartHandler"
	)
