/*
 * ran-alarm-sim.cc
 * ────────────────
 * The simulation CORE of the RAN Alarm Propagation Simulator.
 *
 * A genuine ns-3 LTE radio-access-network scenario — EPC core, eNodeB base
 * stations, UEs, RF propagation, mobility, X2 handover and 3GPP radio-link-
 * failure (RLF) detection. The REAL network events ns-3 produces (RLF, handover
 * failure, RRC connection timeout, random-access error, abnormal context
 * release, low-SINR/interference) are emitted as RAN "events" on stdout, one
 * JSON object per line prefixed with "@@ALARM@@".
 *
 * The Flask control server (sim_server.py) reads those lines and forwards them
 * to Redis; the FastAPI backend then enriches each event into a full alarm
 * record (name / severity / NE type / Next_Alarm) calibrated against the real
 * BT alarm dataset. So: the *trigger, node, timing and SINR are real ns-3*,
 * while the alarm *vocabulary* mirrors the historical dataset.
 *
 * Scenario is read from a simple line-format file (written by sim_server):
 *     NODE <site_id> <x> <y>
 *     EDGE <site_a> <site_b>
 *     UEPERENB <n>
 *     UESPEED <m_per_s>
 *     REALTIME <0|1>
 *
 * Fault injection is polled from a commands file (line format):
 *     inject <site_id> <event_type> [alarm_name with spaces]
 *     link_failure <site_a> <site_b>
 *
 * Build:  placed in ns-3 scratch/, compiled by `./ns3 build`.
 * Run:    ran-alarm-sim --scenario=/data/ns3_scenario.txt
 *                       --commands=/data/ns3_commands.txt --realtime=1
 */

#include "ns3/core-module.h"
#include "ns3/network-module.h"
#include "ns3/internet-module.h"
#include "ns3/mobility-module.h"
#include "ns3/lte-module.h"
#include "ns3/point-to-point-module.h"
#include "ns3/applications-module.h"

#include <csignal>
#include <fstream>
#include <sstream>
#include <map>
#include <vector>
#include <string>
#include <cstdio>
#include <chrono>
#include <thread>

using namespace ns3;

NS_LOG_COMPONENT_DEFINE("RanAlarmSim");

// ─── Global scenario state ──────────────────────────────────────────────────
struct SiteCfg { std::string id; double x; double y; };

static std::vector<SiteCfg>                 g_sites;
static std::vector<std::pair<std::string,std::string>> g_edges;
static std::map<uint16_t, std::string>      g_cellToSite;     // ns-3 cellId → site id
static std::map<std::string, Ptr<LteEnbNetDevice>> g_siteEnb; // site id → eNB device
static std::map<uint16_t, double>           g_cellRsrpDbm;    // latest RSRP per cell (dBm)
static std::map<uint16_t, double>           g_cellLastSinrAlarm; // last sim-time we raised an interference alarm

static std::string g_commandsFile;
static bool        g_realtime = true;
static double      g_enbTxPowerDbm = 43.0;
static double      g_sinrAlarmThreshDb = 5.0;   // raise interference alarm below this SINR (dB) — cell edge
static double      g_sinrAlarmHoldoff  = 3.0;   // min sim-seconds between interference alarms per cell
static double      g_warmupS = 3.0;             // suppress churn alarms during initial cell acquisition

// True during the initial mass-attach transient (a sim artifact, not a real
// network condition) — used to suppress connection-churn alarms at startup.
static bool InWarmup() { return Simulator::Now().GetSeconds() < g_warmupS; }

// Wall-clock pacing: emit events at (sim-seconds / g_speed) wall-seconds apart,
// so the operator sees a live, controllable stream. g_speed = sim-seconds of
// network time compressed into one wall-second. Genuine bursts/cascades (events
// very close in sim-time) stay visually together; long quiet gaps are capped.
static double      g_speed = 30.0;
static const double PACE_CAP_S = 2.0;
static bool        g_paceInit = false;
static double      g_lastEmitSim = 0.0;
static std::chrono::steady_clock::time_point g_lastEmitWall;

static volatile std::sig_atomic_t g_stopRequested = 0;

static void PaceWall(double simSec)
{
    if (!g_realtime) return;
    auto now = std::chrono::steady_clock::now();
    if (!g_paceInit) { g_paceInit = true; g_lastEmitSim = simSec; g_lastEmitWall = now; return; }
    double simGap  = simSec - g_lastEmitSim; if (simGap < 0) simGap = 0;
    double wantS   = simGap / (g_speed > 0.01 ? g_speed : 0.01);
    if (wantS > PACE_CAP_S) wantS = PACE_CAP_S;
    double elapsed = std::chrono::duration<double>(now - g_lastEmitWall).count();
    double toSleep = wantS - elapsed;
    while (toSleep > 0 && !g_stopRequested)
    {
        double chunk = toSleep < 0.1 ? toSleep : 0.1;
        std::this_thread::sleep_for(std::chrono::duration<double>(chunk));
        toSleep -= chunk;
    }
    g_lastEmitWall = std::chrono::steady_clock::now();
    g_lastEmitSim  = simSec;
}

// ─── Helpers ────────────────────────────────────────────────────────────────
static std::string SiteForCell(uint16_t cellId)
{
    auto it = g_cellToSite.find(cellId);
    return (it != g_cellToSite.end()) ? it->second : "unknown";
}

// Minimal JSON string escaping for our (simple) values.
static std::string JEsc(const std::string& s)
{
    std::string o;
    for (char c : s)
    {
        if (c == '"' || c == '\\') { o += '\\'; o += c; }
        else if (c == '\n') o += ' ';
        else o += c;
    }
    return o;
}

// Emit one RAN event as a JSON line consumed by sim_server → Redis → backend.
// The schema matches what backend/alarm_mapper.AlarmMapper.map_event expects.
static void EmitEvent(const std::string& eventType,
                      const std::string& siteId,
                      double sinrDbm,
                      const std::string& extraMetaJson = "",
                      const std::string& alarmName = "")
{
    double simT = Simulator::Now().GetSeconds();
    PaceWall(simT);                        // throttle to wall-clock at g_speed
    std::ostringstream js;
    js << "{"
       << "\"event_type\":\"" << JEsc(eventType) << "\","
       << "\"node_id\":\"" << JEsc(siteId) << "\","
       << "\"source\":\"ns3\","
       << "\"ns3\":true,"
       << "\"sim_time\":" << (simT / 3600.0) << ","          // report in sim-hours
       << "\"sinr_dbm\":" << sinrDbm << ",";
    if (!alarmName.empty())
        js << "\"alarm_name\":\"" << JEsc(alarmName) << "\",";
    js << "\"metadata\":{" << extraMetaJson << "}"
       << "}";
    // Single flushed line; sim_server parses lines beginning with the marker.
    std::cout << "@@ALARM@@ " << js.str() << std::endl;
}

static double SinrForCell(uint16_t cellId)
{
    auto it = g_cellRsrpDbm.find(cellId);
    return (it != g_cellRsrpDbm.end()) ? it->second : -75.0;
}

// ─── ns-3 LTE trace callbacks (REAL network events) ─────────────────────────
static void CbRadioLinkFailure(uint64_t imsi, uint16_t cellId, uint16_t rnti)
{
    std::ostringstream m;
    m << "\"imsi\":" << imsi << ",\"cell_id\":" << cellId << ",\"rnti\":" << rnti
      << ",\"cause\":\"rlf_t310_expired\"";
    EmitEvent("radio_link_failure", SiteForCell(cellId), SinrForCell(cellId), m.str());
}

static void CbHandoverEndError(uint64_t imsi, uint16_t cellId, uint16_t rnti)
{
    std::ostringstream m;
    m << "\"imsi\":" << imsi << ",\"cell_id\":" << cellId << ",\"rnti\":" << rnti;
    EmitEvent("handover_failure", SiteForCell(cellId), SinrForCell(cellId), m.str());
}

static void CbConnectionTimeout(uint64_t imsi, uint16_t cellId, uint16_t rnti, uint8_t /*count*/)
{
    if (InWarmup()) return;
    std::ostringstream m;
    m << "\"imsi\":" << imsi << ",\"cell_id\":" << cellId << ",\"rnti\":" << rnti;
    EmitEvent("rrc_connection_timeout", SiteForCell(cellId), SinrForCell(cellId), m.str());
}

static void CbRandomAccessError(uint64_t imsi, uint16_t cellId, uint16_t rnti)
{
    if (InWarmup()) return;
    std::ostringstream m;
    m << "\"imsi\":" << imsi << ",\"cell_id\":" << cellId << ",\"rnti\":" << rnti;
    EmitEvent("random_access_problem", SiteForCell(cellId), SinrForCell(cellId), m.str());
}

static void CbConnectionReleaseEnb(uint64_t imsi, uint16_t cellId, uint16_t rnti)
{
    if (InWarmup()) return;
    std::ostringstream m;
    m << "\"imsi\":" << imsi << ",\"cell_id\":" << cellId << ",\"rnti\":" << rnti
      << ",\"cause\":\"abnormal_context_release\"";
    EmitEvent("connection_release_abnormal", SiteForCell(cellId), SinrForCell(cellId), m.str());
}

// PHY measurement report: track SINR/RSRP, raise interference alarm on low SINR.
// In ns-3 the reported rsrp is LINEAR power (W) and sinr is a LINEAR ratio.
static void CbReportRsrpSinr(uint16_t cellId, uint16_t rnti, double rsrp, double sinr, uint8_t /*cc*/)
{
    double rsrpDbm = (rsrp > 0) ? 10.0 * std::log10(rsrp) + 30.0 : -140.0;
    double sinrDb  = (sinr > 0) ? 10.0 * std::log10(sinr) : -30.0;
    g_cellRsrpDbm[cellId] = rsrpDbm;

    double simNow = Simulator::Now().GetSeconds();
    if (sinrDb < g_sinrAlarmThreshDb && simNow > g_warmupS)
    {
        double now = simNow;
        double last = g_cellLastSinrAlarm.count(cellId) ? g_cellLastSinrAlarm[cellId] : -1e9;
        if (now - last >= g_sinrAlarmHoldoff)
        {
            g_cellLastSinrAlarm[cellId] = now;
            std::ostringstream m;
            m << "\"rnti\":" << rnti << ",\"rsrp_dbm\":" << rsrpDbm
              << ",\"sinr_db\":" << sinrDb << ",\"threshold_db\":" << g_sinrAlarmThreshDb;
            EmitEvent("sinr_drop", SiteForCell(cellId), rsrpDbm, m.str());
        }
    }
}

// ─── Fault injection (real ns-3 effects) ────────────────────────────────────
static void RestoreTxPower(std::string siteId)
{
    auto it = g_siteEnb.find(siteId);
    if (it != g_siteEnb.end())
        it->second->GetPhy()->SetTxPower(g_enbTxPowerDbm);
}

// Knock an eNB's downlink power down for a few seconds → its UEs really lose
// the radio link (out-of-sync → RLF) and/or hand over, producing genuine ns-3
// alarms. Optionally also emit the operator-requested alarm immediately.
static void InjectFault(const std::string& siteId, const std::string& eventType,
                        const std::string& alarmName)
{
    auto it = g_siteEnb.find(siteId);
    if (it != g_siteEnb.end())
    {
        it->second->GetPhy()->SetTxPower(1.0);   // near-blackout
        Simulator::Schedule(Seconds(5.0), &RestoreTxPower, siteId);
    }
    // Surface the operator's intended alarm right away (manual marker).
    if (!alarmName.empty())
        EmitEvent("manual_injection", siteId, SinrForCell(0), "\"injected\":true", alarmName);
    else
        EmitEvent(eventType, siteId, -90.0, "\"injected\":true");
}

static void InjectLinkFailure(const std::string& a, const std::string& b)
{
    // Real effect: black out the target eNB; emit backhaul markers on both ends.
    auto it = g_siteEnb.find(b);
    if (it != g_siteEnb.end())
    {
        it->second->GetPhy()->SetTxPower(1.0);
        Simulator::Schedule(Seconds(5.0), &RestoreTxPower, b);
    }
    EmitEvent("backhaul_link_failure", a, -85.0, "\"peer\":\"" + JEsc(b) + "\"");
    EmitEvent("s1_interface_failure", b, -90.0, "\"cause\":\"backhaul_loss_from_" + JEsc(a) + "\"");
}

// ─── Command + lifecycle polling (scheduled events) ─────────────────────────
static void PollCommands()
{
    std::ifstream f(g_commandsFile);
    if (f.good())
    {
        std::vector<std::string> lines;
        std::string line;
        while (std::getline(f, line))
            if (!line.empty()) lines.push_back(line);
        f.close();
        std::remove(g_commandsFile.c_str());

        for (auto& l : lines)
        {
            std::istringstream ss(l);
            std::string cmd; ss >> cmd;
            if (cmd == "inject")
            {
                std::string site, evt; ss >> site >> evt;
                std::string rest, name;
                std::getline(ss, rest);
                if (!rest.empty() && rest[0] == ' ') name = rest.substr(1);
                InjectFault(site, evt.empty() ? "cell_blocked" : evt, name);
            }
            else if (cmd == "link_failure")
            {
                std::string a, b; ss >> a >> b;
                InjectLinkFailure(a, b);
            }
            else if (cmd == "set_speed")
            {
                double v; ss >> v;
                if (v > 0) g_speed = v;
            }
        }
    }

    if (g_stopRequested)
    {
        Simulator::Stop();
        return;
    }
    Simulator::Schedule(Seconds(0.5), &PollCommands);
}

static void SigHandler(int) { g_stopRequested = 1; }

// ─── Scenario file parsing ──────────────────────────────────────────────────
static int g_uePerEnb = 3;
static double g_ueSpeed = 20.0;

static bool LoadScenario(const std::string& path)
{
    std::ifstream f(path);
    if (!f.good()) { std::cerr << "[ranalarm] cannot open scenario " << path << std::endl; return false; }
    std::string line;
    while (std::getline(f, line))
    {
        std::istringstream ss(line);
        std::string kw; ss >> kw;
        if (kw == "NODE") { SiteCfg s; ss >> s.id >> s.x >> s.y; g_sites.push_back(s); }
        else if (kw == "EDGE") { std::string a, b; ss >> a >> b; g_edges.push_back({a, b}); }
        else if (kw == "UEPERENB") ss >> g_uePerEnb;
        else if (kw == "UESPEED") ss >> g_ueSpeed;
        else if (kw == "SPEED") ss >> g_speed;
        else if (kw == "REALTIME") { int r; ss >> r; g_realtime = (r != 0); }
    }
    return !g_sites.empty();
}

// ─── Main ───────────────────────────────────────────────────────────────────
int main(int argc, char* argv[])
{
    std::string scenarioFile = "/data/ns3_scenario.txt";
    g_commandsFile           = "/data/ns3_commands.txt";
    double simTimeS          = 3600.0;   // batch stop time (ignored-ish in realtime)
    int    realtimeFlag      = 1;

    CommandLine cmd;
    cmd.AddValue("scenario", "Scenario file path", scenarioFile);
    cmd.AddValue("commands", "Commands file path", g_commandsFile);
    cmd.AddValue("simTime",  "Stop time (s)", simTimeS);
    cmd.AddValue("realtime", "1=realtime paced, 0=as-fast-as-possible", realtimeFlag);
    cmd.Parse(argc, argv);

    if (!LoadScenario(scenarioFile))
    {
        std::cerr << "[ranalarm] no scenario / no nodes — aborting" << std::endl;
        return 1;
    }
    g_realtime = (realtimeFlag != 0) && g_realtime;
    // Pacing is done in software (PaceWall) so the default simulator runs the
    // LTE model as fast as the CPU allows, then we throttle event *emission* to
    // wall-clock at g_speed. This decouples alarm cadence from ns-3's heavy
    // per-subframe scheduling (RealtimeSimulatorImpl can't keep up at 1×).

    uint16_t numEnb = g_sites.size();
    uint16_t numUe  = numEnb * g_uePerEnb;

    std::cerr << "[ranalarm] scenario: " << numEnb << " eNBs, " << numUe
              << " UEs, realtime=" << g_realtime << ", ueSpeed=" << g_ueSpeed << std::endl;

    // ── RLF detection + radio config (mirrors lena-radio-link-failure example) ──
    Config::SetDefault("ns3::LteHelper::UseIdealRrc", BooleanValue(false));
    Config::SetDefault("ns3::LteSpectrumPhy::CtrlErrorModelEnabled", BooleanValue(true));
    Config::SetDefault("ns3::LteSpectrumPhy::DataErrorModelEnabled", BooleanValue(true));
    Config::SetDefault("ns3::LteRlcUm::MaxTxBufferSize", UintegerValue(60 * 1024));
    Config::SetDefault("ns3::LteEnbPhy::TxPower", DoubleValue(g_enbTxPowerDbm));
    Config::SetDefault("ns3::LteUePhy::TxPower", DoubleValue(23));
    Config::SetDefault("ns3::LteUePhy::EnableRlfDetection", BooleanValue(true));
    // Make RLF reasonably reachable at cell edge (3GPP-valid values).
    Config::SetDefault("ns3::LteUeRrc::N310", UintegerValue(2));
    Config::SetDefault("ns3::LteUeRrc::N311", UintegerValue(1));
    Config::SetDefault("ns3::LteUeRrc::T310", TimeValue(MilliSeconds(500)));

    Ptr<LteHelper> lteHelper = CreateObject<LteHelper>();
    Ptr<PointToPointEpcHelper> epcHelper = CreateObject<PointToPointEpcHelper>();
    lteHelper->SetEpcHelper(epcHelper);

    lteHelper->SetPathlossModelType(TypeId::LookupByName("ns3::LogDistancePropagationLossModel"));
    lteHelper->SetPathlossModelAttribute("Exponent", DoubleValue(3.9));
    lteHelper->SetPathlossModelAttribute("ReferenceLoss", DoubleValue(38.57));
    lteHelper->SetPathlossModelAttribute("ReferenceDistance", DoubleValue(1));
    lteHelper->SetSchedulerType("ns3::PfFfMacScheduler");
    lteHelper->SetHandoverAlgorithmType("ns3::A3RsrpHandoverAlgorithm");
    lteHelper->SetEnbDeviceAttribute("DlBandwidth", UintegerValue(25));
    lteHelper->SetEnbDeviceAttribute("UlBandwidth", UintegerValue(25));

    // ── Internet / remote host ──
    Ptr<Node> pgw = epcHelper->GetPgwNode();
    NodeContainer remoteHostContainer; remoteHostContainer.Create(1);
    Ptr<Node> remoteHost = remoteHostContainer.Get(0);
    InternetStackHelper internet; internet.Install(remoteHostContainer);
    PointToPointHelper p2ph;
    p2ph.SetDeviceAttribute("DataRate", DataRateValue(DataRate("100Gb/s")));
    p2ph.SetDeviceAttribute("Mtu", UintegerValue(1500));
    p2ph.SetChannelAttribute("Delay", TimeValue(MilliSeconds(10)));
    NetDeviceContainer internetDevices = p2ph.Install(pgw, remoteHost);
    Ipv4AddressHelper ipv4h; ipv4h.SetBase("1.0.0.0", "255.0.0.0");
    Ipv4InterfaceContainer internetIpIfaces = ipv4h.Assign(internetDevices);
    Ipv4Address remoteHostAddr = internetIpIfaces.GetAddress(1);
    Ipv4StaticRoutingHelper ipv4RoutingHelper;
    Ptr<Ipv4StaticRouting> remoteHostStaticRouting =
        ipv4RoutingHelper.GetStaticRouting(remoteHost->GetObject<Ipv4>());
    remoteHostStaticRouting->AddNetworkRouteTo(Ipv4Address("7.0.0.0"), Ipv4Mask("255.0.0.0"), 1);

    // ── eNB nodes at topology positions ──
    NodeContainer enbNodes; enbNodes.Create(numEnb);
    Ptr<ListPositionAllocator> enbPos = CreateObject<ListPositionAllocator>();
    double minx = 1e9, maxx = -1e9, miny = 1e9, maxy = -1e9;
    for (auto& s : g_sites)
    {
        double X = s.x * 5.0, Y = s.y * 5.0;   // canvas px → metres
        enbPos->Add(Vector(X, Y, 30.0));
        minx = std::min(minx, X); maxx = std::max(maxx, X);
        miny = std::min(miny, Y); maxy = std::max(maxy, Y);
    }
    MobilityHelper enbMob;
    enbMob.SetMobilityModel("ns3::ConstantPositionMobilityModel");
    enbMob.SetPositionAllocator(enbPos);
    enbMob.Install(enbNodes);

    // ── UE nodes roaming across the served area (drives handover + RLF) ──
    NodeContainer ueNodes; ueNodes.Create(numUe);
    Ptr<ListPositionAllocator> uePos = CreateObject<ListPositionAllocator>();
    for (uint16_t i = 0; i < numUe; i++)
    {
        auto& home = g_sites[i % numEnb];
        uePos->Add(Vector(home.x * 5.0 + (i % 5) * 20.0, home.y * 5.0 + (i % 3) * 20.0, 1.5));
    }
    double pad = 150.0;
    MobilityHelper ueMob;
    ueMob.SetMobilityModel(
        "ns3::RandomWalk2dMobilityModel",
        "Bounds", RectangleValue(Rectangle(minx - pad, maxx + pad, miny - pad, maxy + pad)),
        "Distance", DoubleValue(80.0),
        "Speed", StringValue("ns3::ConstantRandomVariable[Constant=" + std::to_string(g_ueSpeed) + "]"));
    ueMob.SetPositionAllocator(uePos);
    ueMob.Install(ueNodes);

    // ── Install LTE devices ──
    NetDeviceContainer enbDevs = lteHelper->InstallEnbDevice(enbNodes);
    NetDeviceContainer ueDevs  = lteHelper->InstallUeDevice(ueNodes);
    int64_t stream = 1;
    stream += lteHelper->AssignStreams(enbDevs, stream);
    stream += lteHelper->AssignStreams(ueDevs, stream);

    // Map ns-3 cellId → topology site id, and remember each eNB device.
    for (uint16_t i = 0; i < numEnb; i++)
    {
        Ptr<LteEnbNetDevice> enb = enbDevs.Get(i)->GetObject<LteEnbNetDevice>();
        g_cellToSite[enb->GetCellId()] = g_sites[i].id;
        g_siteEnb[g_sites[i].id] = enb;
    }

    // ── IP stack + attach + default routes ──
    internet.Install(ueNodes);
    Ipv4InterfaceContainer ueIpIfaces = epcHelper->AssignUeIpv4Address(NetDeviceContainer(ueDevs));
    for (uint16_t u = 0; u < numUe; u++)
    {
        Ptr<Ipv4StaticRouting> ueRouting =
            ipv4RoutingHelper.GetStaticRouting(ueNodes.Get(u)->GetObject<Ipv4>());
        ueRouting->SetDefaultRoute(epcHelper->GetUeDefaultGatewayAddress(), 1);
        // Attach each UE to its home eNB; handover algorithm moves it later.
        lteHelper->Attach(ueDevs.Get(u), enbDevs.Get(u % numEnb));
    }

    lteHelper->AddX2Interface(enbNodes);

    // ── Downlink traffic so RRC connections are real and bearers active ──
    uint16_t dlPort = 10000;
    for (uint16_t u = 0; u < numUe; u++)
    {
        UdpClientHelper dlClient(ueIpIfaces.GetAddress(u), dlPort);
        dlClient.SetAttribute("Interval", TimeValue(MilliSeconds(50)));
        dlClient.SetAttribute("PacketSize", UintegerValue(1024));
        dlClient.SetAttribute("MaxPackets", UintegerValue(0xFFFFFFFF));
        ApplicationContainer app = dlClient.Install(remoteHost);
        PacketSinkHelper sink("ns3::UdpSocketFactory",
                              InetSocketAddress(Ipv4Address::GetAny(), dlPort));
        ApplicationContainer sinkApp = sink.Install(ueNodes.Get(u));
        app.Start(MilliSeconds(300));
        sinkApp.Start(MilliSeconds(200));
    }

    // ── Connect REAL LTE trace sources to alarm emitters ──
    Config::ConnectWithoutContext("/NodeList/*/DeviceList/*/LteUeRrc/RadioLinkFailure",
                                  MakeCallback(&CbRadioLinkFailure));
    Config::ConnectWithoutContext("/NodeList/*/DeviceList/*/LteUeRrc/HandoverEndError",
                                  MakeCallback(&CbHandoverEndError));
    Config::ConnectWithoutContext("/NodeList/*/DeviceList/*/LteUeRrc/ConnectionTimeout",
                                  MakeCallback(&CbConnectionTimeout));
    Config::ConnectWithoutContext("/NodeList/*/DeviceList/*/LteUeRrc/RandomAccessError",
                                  MakeCallback(&CbRandomAccessError));
    Config::ConnectWithoutContext("/NodeList/*/DeviceList/*/LteEnbRrc/NotifyConnectionRelease",
                                  MakeCallback(&CbConnectionReleaseEnb));
    Config::ConnectWithoutContext("/NodeList/*/DeviceList/*/ComponentCarrierMapUe/*/LteUePhy/"
                                  "ReportCurrentCellRsrpSinr",
                                  MakeCallback(&CbReportRsrpSinr));

    // ── Lifecycle: command polling + SIGTERM handling ──
    std::signal(SIGTERM, &SigHandler);
    std::signal(SIGINT,  &SigHandler);
    Simulator::Schedule(Seconds(1.0), &PollCommands);

    std::cerr << "[ranalarm] starting ns-3 LTE simulation core" << std::endl;
    std::cout << "@@READY@@ ns3 LTE core: " << numEnb << " eNBs, " << numUe << " UEs" << std::endl;

    Simulator::Stop(Seconds(g_realtime ? 1e7 : simTimeS));
    Simulator::Run();
    Simulator::Destroy();
    std::cerr << "[ranalarm] simulation stopped" << std::endl;
    return 0;
}
