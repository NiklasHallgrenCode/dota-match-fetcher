package fetcher;

import skadistats.clarity.io.Util;
import skadistats.clarity.model.DTClass;
import skadistats.clarity.model.Entity;
import skadistats.clarity.model.FieldPath;
import skadistats.clarity.processor.entities.Entities;
import skadistats.clarity.processor.entities.OnEntityCreated;
import skadistats.clarity.processor.entities.OnEntityDeleted;
import skadistats.clarity.processor.entities.UsesEntities;
import skadistats.clarity.processor.reader.OnMessage;
import skadistats.clarity.processor.reader.OnTickStart;
import skadistats.clarity.processor.runner.Context;
import skadistats.clarity.processor.runner.SimpleRunner;
import skadistats.clarity.source.MappedFileSource;
import skadistats.clarity.wire.shared.common.proto.CommonNetworkBaseTypes.CNETMsg_Tick;
import skadistats.clarity.wire.shared.demo.proto.Demo.CDemoFileInfo;

import java.io.FileWriter;
import java.io.IOException;
import java.io.Writer;
import java.util.ArrayList;
import java.util.List;
import java.util.function.Predicate;

/**
 * Reads a Dota 2 .dem replay and writes the raw data the analysis needs as JSON:
 * the players, the winner, and one sample per player per second of game time
 * (position, net worth, earned gold, last hits) for the first minutes of the game.
 *
 * Usage: java -jar extractor.jar <replay.dem> <out.json>
 */
public class Extract {

    // Game time (seconds after the horn) to sample up to
    static final int SAMPLE_UNTIL = 600;
    static final int NUM_PLAYERS = 10;

    static final String SINGLETONS = "CDOTAGamerulesProxy|CDOTA_PlayerResource|CDOTA_DataDire|CDOTA_DataRadiant";

    static final Predicate<DTClass> ENTITY_FILTER = dt -> {
        String n = dt.getDtName();
        return n.matches(SINGLETONS) || n.startsWith("CDOTA_Unit_Hero_");
    };

    Entity gameRules, playerResource, dataRadiant, dataDire;
    int serverTick;
    int nextSample = 0;

    // Index into m_vecPlayerData for each of the 10 players, radiant first
    int[] playerIndex;
    long[] steamIds = new long[NUM_PLAYERS];
    int[] teams = new int[NUM_PLAYERS];
    int[] teamSlots = new int[NUM_PLAYERS];
    int[] heroIds = new int[NUM_PLAYERS];
    List<String> samples = new ArrayList<>();

    CDemoFileInfo fileInfo;

    public static void main(String[] args) throws Exception {
        Extract extract = new Extract();
        SimpleRunner runner = new SimpleRunner(new MappedFileSource(args[0]));
        runner.withEntityFilter(ENTITY_FILTER);
        runner.runWith(extract);
        try (Writer w = new FileWriter(args[1])) {
            w.write(extract.toJson());
        }
    }

    @OnEntityCreated(classPattern = SINGLETONS)
    public void onSingletonCreated(Context ctx, Entity e) {
        switch (e.getDtClass().getDtName()) {
            case "CDOTAGamerulesProxy" -> gameRules = e;
            case "CDOTA_PlayerResource" -> playerResource = e;
            case "CDOTA_DataRadiant" -> dataRadiant = e;
            case "CDOTA_DataDire" -> dataDire = e;
        }
    }

    @OnEntityDeleted(classPattern = SINGLETONS)
    public void onSingletonDeleted(Context ctx, Entity e) {
        if (gameRules == e) gameRules = null;
        if (playerResource == e) playerResource = null;
        if (dataRadiant == e) dataRadiant = null;
        if (dataDire == e) dataDire = null;
    }

    @OnMessage(CNETMsg_Tick.class)
    public void onTick(CNETMsg_Tick message) {
        serverTick = message.getTick();
    }

    @OnMessage(CDemoFileInfo.class)
    public void onFileInfo(Context ctx, CDemoFileInfo message) {
        fileInfo = message;
    }

    @UsesEntities
    @OnTickStart
    public void onTickStart(Context ctx, boolean synthetic) {
        if (gameRules == null || playerResource == null || dataRadiant == null || dataDire == null) {
            return;
        }
        Float startTime = prop(gameRules, "m_pGameRules.m_flGameStartTime");
        if (startTime == null || startTime == 0) {
            return;
        }
        int clock = Math.round(gameTime() - startTime);
        if (clock < nextSample || nextSample > SAMPLE_UNTIL) {
            return;
        }
        if (playerIndex == null && !findPlayers()) {
            return;
        }
        Entities entities = ctx.getProcessor(Entities.class);
        StringBuilder sb = new StringBuilder("[").append(nextSample);
        for (int p = 0; p < NUM_PLAYERS; p++) {
            int i = playerIndex[p];
            Entity data = teams[p] == 2 ? dataRadiant : dataDire;
            String d = "m_vecDataTeam." + Util.arrayIdxToString(teamSlots[p]) + ".";
            Integer heroId = prop(playerResource, "m_vecPlayerTeamData." + Util.arrayIdxToString(i) + ".m_nSelectedHeroID");
            Integer handle = prop(playerResource, "m_vecPlayerTeamData." + Util.arrayIdxToString(i) + ".m_hSelectedHero");
            if (heroId != null && heroId > 0) {
                heroIds[p] = heroId;
            }
            Float x = null, y = null;
            Integer lifeState = null;
            Entity hero = handle == null ? null : entities.getByHandle(handle);
            if (hero != null) {
                x = position(hero, "X");
                y = position(hero, "Y");
                lifeState = prop(hero, "m_lifeState");
            }
            // [x, y, alive, net worth, earned gold, last hits, denies]
            sb.append(",[").append(num(x)).append(',').append(num(y)).append(',')
              .append(lifeState == null ? "null" : lifeState == 0 ? "1" : "0").append(',')
              .append(num(prop(data, d + "m_iNetWorth"))).append(',')
              .append(num(prop(data, d + "m_iTotalEarnedGold"))).append(',')
              .append(num(prop(data, d + "m_iLastHitCount"))).append(',')
              .append(num(prop(data, d + "m_iDenyCount"))).append(']');
        }
        samples.add(sb.append(']').toString());
        nextSample = clock + 1;
    }

    /** Game time in seconds, excluding pauses. */
    float gameTime() {
        Float t = prop(gameRules, "m_pGameRules.m_fGameTime");
        if (t != null) {
            return t;
        }
        // Since 7.32e the game time has to be computed from ticks (30 per second)
        Boolean paused = prop(gameRules, "m_pGameRules.m_bGamePaused");
        Integer pauseStart = prop(gameRules, "m_pGameRules.m_nPauseStartTick");
        Integer pausedTicks = prop(gameRules, "m_pGameRules.m_nTotalPausedTicks");
        int tick = Boolean.TRUE.equals(paused) && pauseStart != null ? pauseStart : serverTick;
        return (tick - (pausedTicks == null ? 0 : pausedTicks)) / 30f;
    }

    /** Finds the 10 players on radiant (team 2) and dire (team 3), skipping coaches and spectators. */
    boolean findPlayers() {
        int[] idx = new int[NUM_PLAYERS];
        int found = 0;
        for (int i = 0; i < 64 && found < NUM_PLAYERS; i++) {
            String p = "m_vecPlayerData." + Util.arrayIdxToString(i) + ".";
            Integer team = prop(playerResource, p + "m_iPlayerTeam");
            if (team == null) {
                break;
            }
            if (team == 2 || team == 3) {
                Integer teamSlot = prop(playerResource, "m_vecPlayerTeamData." + Util.arrayIdxToString(i) + ".m_iTeamSlot");
                Long steamId = prop(playerResource, p + "m_iPlayerSteamID");
                if (teamSlot == null || teamSlot < 0) {
                    return false;
                }
                idx[found] = i;
                teams[found] = team;
                teamSlots[found] = teamSlot;
                steamIds[found] = steamId == null ? 0 : steamId;
                found++;
            }
        }
        if (found < NUM_PLAYERS) {
            return false;
        }
        playerIndex = idx;
        return true;
    }

    Float position(Entity e, String axis) {
        Integer cell = prop(e, "CBodyComponent.m_cell" + axis);
        Float vec = prop(e, "CBodyComponent.m_vec" + axis);
        if (cell == null || vec == null) {
            return null;
        }
        return (cell * 128.0f + vec) / 128;
    }

    @SuppressWarnings("unchecked")
    static <T> T prop(Entity e, String name) {
        try {
            FieldPath fp = e.getFieldPathForName(name);
            return fp == null ? null : (T) e.getPropertyForFieldPath(fp);
        } catch (Exception ex) {
            return null;
        }
    }

    static String num(Object o) {
        if (o == null) {
            return "null";
        }
        if (o instanceof Float f) {
            return String.format(java.util.Locale.ROOT, "%.2f", f);
        }
        return o.toString();
    }

    static String str(String s) {
        StringBuilder sb = new StringBuilder("\"");
        for (char c : s.toCharArray()) {
            if (c == '"' || c == '\\') {
                sb.append('\\').append(c);
            } else if (c < 0x20) {
                sb.append(String.format("\\u%04x", (int) c));
            } else {
                sb.append(c);
            }
        }
        return sb.append('"').toString();
    }

    String toJson() {
        StringBuilder sb = new StringBuilder("{");
        if (fileInfo != null && fileInfo.hasGameInfo()) {
            var dota = fileInfo.getGameInfo().getDota();
            sb.append("\"match_id\":").append(dota.getMatchId())
              .append(",\"game_mode\":").append(dota.getGameMode())
              .append(",\"game_winner\":").append(dota.getGameWinner())
              .append(",\"file_players\":[");
            for (int i = 0; i < dota.getPlayerInfoCount(); i++) {
                var pi = dota.getPlayerInfo(i);
                sb.append(i > 0 ? "," : "").append("{\"steamid\":").append(pi.getSteamid())
                  .append(",\"team\":").append(pi.getGameTeam())
                  .append(",\"hero\":").append(str(pi.getHeroName()))
                  .append(",\"name\":").append(str(pi.getPlayerName())).append('}');
            }
            sb.append("],");
        }
        sb.append("\"players\":[");
        for (int p = 0; p < NUM_PLAYERS && playerIndex != null; p++) {
            sb.append(p > 0 ? "," : "").append("{\"steamid\":").append(steamIds[p])
              .append(",\"team\":").append(teams[p])
              .append(",\"team_slot\":").append(teamSlots[p])
              .append(",\"hero_id\":").append(heroIds[p]).append('}');
        }
        sb.append("],\"sample_fields\":[\"x\",\"y\",\"alive\",\"networth\",\"gold\",\"lh\",\"denies\"],\"samples\":[");
        sb.append(String.join(",\n", samples));
        return sb.append("]}\n").toString();
    }
}
