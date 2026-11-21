// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

/// @title Honeybee Quorum Consensus v2 (HQC2): BIM change approval with action-level accountability
/// @notice Research prototype. Scores are 0..1000, ratios are basis points (10000 = 100%).
/// @dev v2 adds stakes, evidence challenges and slashing. A challenge disputes the score an agent
///      endorsed. Only scores of verifiable criteria (deterministic checks: structural, MEP, cost,
///      sustainability) can be challenged; architectural and client judgements cannot.
///      The re-computed score is submitted by an auditor (in practice a reproducible checking
///      service or a multi-signature committee). If it differs from the claimed score by more
///      than `tolerance`, the challenge is upheld: the agent's support and stop-signal cuts are
///      reversed, part of its stake is slashed and it is suspended for that request.
contract HQC2 {
    enum Role { None, Architect, Structural, MEP, Cost, Sustainability, Client }
    enum State { None, Evaluating, CommitWindow, Committed, Escalated }
    enum CState { None, Open, Upheld, Rejected }

    struct Agent { Role role; uint32 weight; bool active; uint256 stake; uint32 openRequests; }

    struct Request {
        address proposer;
        State state;
        uint16 round;
        uint8 k;
        uint8 candidate;
        uint16 openChallenges;
        uint64 windowEnd;
        bytes32[] altHashes;
    }

    struct Endorsement { uint8 altPlus1; uint16 score; uint16 lastRound; bool slashed; bool released; bool counted; uint256 added; bytes32 evidence; }

    struct Challenge {
        uint256 cId;
        address challenger;
        address accused;
        uint16 claimedScore;   // challenger's re-computed score
        uint16 accusedScore;   // score the accused had endorsed when challenged
        uint16 auditedScore;   // auditor's re-computed score
        CState state;
        uint256 bond;
        bytes32 evidence;      // hash of the challenger's re-computation report
    }

    address public owner;
    address public auditor;
    uint256 public totalWeight;

    // protocol parameters
    uint16 public thetaBps;
    uint16 public alphaBps;
    uint16 public sigmaBps;
    uint16 public capBps;
    uint16 public maxRounds;
    uint32 public windowSeconds;
    uint8  public mandatoryMask;
    uint8  public vetoMask;
    uint8  public verifiableMask;
    uint16 public tolerance;       // score units (0..1000)
    uint16 public slashBps;
    uint256 public minStake;
    uint256 public challengeBond;

    mapping(address => Agent) public agents;
    mapping(uint256 => Request) private requests;
    uint256 public requestCount;

    mapping(uint256 => mapping(uint8 => uint256)) public support;
    mapping(uint256 => mapping(uint8 => uint256)) public rawAdded;     // undecayed sum of additions
    mapping(uint256 => mapping(uint8 => uint256)) public scoreSum;
    mapping(uint256 => mapping(uint8 => uint256)) public scoreCount;
    mapping(uint256 => mapping(uint8 => mapping(uint8 => uint16))) public roleCount; // cId, alt, role
    mapping(uint256 => mapping(address => Endorsement)) public endorsementOf;
    mapping(uint256 => mapping(address => uint16)) public lastStopRound;
    mapping(uint256 => mapping(address => mapping(uint8 => uint256))) public cutBy; // cId, agent, target
    mapping(uint256 => mapping(address => bool)) public suspended;

    mapping(uint256 => Challenge) public challenges;
    uint256 public challengeCount;
    mapping(address => uint256) public credit;     // pull payments
    uint256 public treasury;

    event ChangeProposed(uint256 indexed cId, address proposer, uint8 k);
    event Abandoned(uint256 indexed cId, address agent);
    event Endorsed(uint256 indexed cId, address agent, uint8 alt, uint16 score, bytes32 evidence);
    event StopSignal(uint256 indexed cId, address agent, uint8 target, uint256 cut, bytes32 evidence);
    event QuorumReached(uint256 indexed cId, uint8 alt, uint64 windowEnd);
    event Vetoed(uint256 indexed cId, address agent, bytes32 reason);
    event Committed(uint256 indexed cId, uint8 alt, bytes32 altHash);
    event Escalated(uint256 indexed cId, uint16 round);
    event Challenged(uint256 indexed chId, uint256 indexed cId, address challenger, address accused, uint16 claimedScore, bytes32 evidence);
    event ChallengeResolved(uint256 indexed chId, bool upheld, uint16 auditedScore, uint256 slashed);

    modifier onlyOwner() { require(msg.sender == owner, "owner"); _; }
    modifier onlyAuditor() { require(msg.sender == auditor, "auditor"); _; }
    modifier onlyAgent() { require(agents[msg.sender].active, "not agent"); _; }

    struct Params {
        uint16 thetaBps; uint16 alphaBps; uint16 sigmaBps; uint16 capBps;
        uint16 maxRounds; uint32 windowSeconds;
        uint8 mandatoryMask; uint8 vetoMask; uint8 verifiableMask;
        uint16 tolerance; uint16 slashBps; uint256 minStake; uint256 challengeBond;
    }

    constructor(Params memory p, address _auditor) {
        require(p.tolerance <= 1000 && p.slashBps <= 10000 && p.thetaBps <= 10000, "params");
        owner = msg.sender;
        auditor = _auditor;
        thetaBps = p.thetaBps; alphaBps = p.alphaBps; sigmaBps = p.sigmaBps; capBps = p.capBps;
        maxRounds = p.maxRounds; windowSeconds = p.windowSeconds;
        mandatoryMask = p.mandatoryMask; vetoMask = p.vetoMask; verifiableMask = p.verifiableMask;
        tolerance = p.tolerance; slashBps = p.slashBps; minStake = p.minStake; challengeBond = p.challengeBond;
    }

    // ------------------------------------------------------------------ membership and stake
    function registerAgent(address a, Role role, uint32 weight) external onlyOwner {
        require(!agents[a].active && role != Role.None && weight > 0, "bad agent");
        agents[a] = Agent(role, weight, true, 0, 0);
        totalWeight += weight;
    }

    function deposit() external payable onlyAgent {
        agents[msg.sender].stake += msg.value;
    }

    /// @notice Stake can only be withdrawn when the agent has no endorsement in an unfinished request.
    function withdraw(uint256 amount) external onlyAgent {
        Agent storage a = agents[msg.sender];
        require(a.openRequests == 0, "open requests");
        require(amount <= a.stake, "amount");
        a.stake -= amount;
        credit[msg.sender] += amount;
    }

    function claim() external {
        uint256 v = credit[msg.sender];
        require(v > 0, "nothing");
        credit[msg.sender] = 0;
        (bool ok, ) = payable(msg.sender).call{value: v}("");
        require(ok, "transfer");
    }

    // ------------------------------------------------------------------ protocol
    function proposeChange(bytes32[] calldata altHashes) external onlyAgent returns (uint256 cId) {
        require(altHashes.length >= 2 && altHashes.length <= 8, "k");
        cId = ++requestCount;
        Request storage r = requests[cId];
        r.proposer = msg.sender;
        r.state = State.Evaluating;
        r.k = uint8(altHashes.length);
        r.altHashes = altHashes;
        emit ChangeProposed(cId, msg.sender, r.k);
    }

    /// @notice Waggle dance: endorsement weighted by evaluated quality, backed by stake and evidence.
    ///         An agent may re-dance once per round: its previous (decayed) endorsement is withdrawn
    ///         and the new one is added at full strength, so active support is refreshed while
    ///         stale support fades through the decay in tick().
    function endorse(uint256 cId, uint8 alt, uint16 score, bytes32 evidence) external onlyAgent {
        Request storage r = requests[cId];
        require(r.state == State.Evaluating, "state");
        require(alt < r.k && score <= 1000 && evidence != bytes32(0), "input");
        require(!suspended[cId][msg.sender], "suspended");
        Endorsement storage e = endorsementOf[cId][msg.sender];
        require(e.lastRound != r.round + 1, "once per round");
        Agent storage a = agents[msg.sender];
        require(a.stake >= minStake, "stake");
        if (e.altPlus1 != 0) _withdraw(cId, msg.sender, e);
        uint256 add = uint256(a.weight) * score;
        uint256 cap = totalWeight * 1000 * capBps / 10000;
        if (add > cap) add = cap;
        support[cId][alt] += add;
        rawAdded[cId][alt] += add;
        scoreSum[cId][alt] += score;
        scoreCount[cId][alt] += 1;
        roleCount[cId][alt][uint8(a.role)] += 1;
        e.altPlus1 = alt + 1; e.score = score; e.added = add; e.evidence = evidence; e.lastRound = r.round + 1;
        if (!e.counted) { e.counted = true; a.openRequests += 1; }
        emit Endorsed(cId, msg.sender, alt, score, evidence);
    }

    /// @notice Abandonment: the agent withdraws its endorsement (a bee that stops dancing).
    function abandon(uint256 cId) external onlyAgent {
        require(requests[cId].state == State.Evaluating, "state");
        Endorsement storage e = endorsementOf[cId][msg.sender];
        require(e.altPlus1 != 0 && !e.slashed, "no endorsement");
        _withdraw(cId, msg.sender, e);
        emit Abandoned(cId, msg.sender);
    }

    /// @dev Removes an endorsement's remaining contribution, in proportion to decay since it was added.
    function _withdraw(uint256 cId, address who, Endorsement storage e) internal {
        uint8 alt = e.altPlus1 - 1;
        uint256 raw = rawAdded[cId][alt];
        uint256 rem = raw == 0 ? 0 : support[cId][alt] * e.added / raw;
        support[cId][alt] -= rem;
        rawAdded[cId][alt] = raw - e.added;
        scoreSum[cId][alt] -= e.score;
        scoreCount[cId][alt] -= 1;
        roleCount[cId][alt][uint8(agents[who].role)] -= 1;
        e.altPlus1 = 0;
        e.added = 0;
    }

    /// @notice Stop signal: cross-inhibition of a rival alternative, once per round.
    function stopSignal(uint256 cId, uint8 target, bytes32 evidence) external onlyAgent {
        Request storage r = requests[cId];
        require(r.state == State.Evaluating, "state");
        require(!suspended[cId][msg.sender], "suspended");
        uint8 own = endorsementOf[cId][msg.sender].altPlus1;
        require(own != 0 && own - 1 != target && target < r.k, "target");
        require(evidence != bytes32(0), "evidence");
        require(lastStopRound[cId][msg.sender] != r.round + 1, "once per round");
        lastStopRound[cId][msg.sender] = r.round + 1;
        uint256 cut = support[cId][target] * sigmaBps * agents[msg.sender].weight / (10000 * totalWeight);
        support[cId][target] -= cut;
        cutBy[cId][msg.sender][target] += cut;
        emit StopSignal(cId, msg.sender, target, cut, evidence);
    }

    function _roleMask(uint256 cId, uint8 alt) internal view returns (uint8 m) {
        for (uint8 role = 1; role <= 6; role++) {
            if (roleCount[cId][alt][role] > 0) m |= (uint8(1) << role);
        }
    }

    /// @notice Round step: quality-dependent decay, then quorum check.
    function tick(uint256 cId) external {
        Request storage r = requests[cId];
        require(r.state == State.Evaluating, "state");
        r.round += 1;
        uint8 best; uint256 bestS;
        for (uint8 i = 0; i < r.k; i++) {
            uint256 s = support[cId][i];
            if (s == 0) continue;
            uint256 q = scoreCount[cId][i] == 0 ? 0 : scoreSum[cId][i] / scoreCount[cId][i];
            uint256 d = s * alphaBps * (1000 - q) / (10000 * 1000);
            s -= d;
            support[cId][i] = s;
            if (s > bestS) { bestS = s; best = i; }
        }
        uint256 quorum = totalWeight * 1000 * thetaBps / 10000;
        bool rolesOk = (_roleMask(cId, best) & mandatoryMask) == mandatoryMask;
        if (bestS >= quorum && rolesOk) {
            r.state = State.CommitWindow;
            r.candidate = best;
            r.windowEnd = uint64(block.timestamp + windowSeconds);
            emit QuorumReached(cId, best, r.windowEnd);
        } else if (r.round >= maxRounds) {
            r.state = State.Escalated;
            emit Escalated(cId, r.round);
        }
    }

    function veto(uint256 cId, bytes32 reason) external onlyAgent {
        Request storage r = requests[cId];
        require(r.state == State.CommitWindow && block.timestamp < r.windowEnd, "window");
        require(((vetoMask >> uint8(agents[msg.sender].role)) & uint8(1)) == uint8(1), "no veto right");
        require(reason != bytes32(0), "reason");
        // a veto reopens evaluation and halves the candidate's support (a strong, evidenced stop signal)
        support[cId][r.candidate] = support[cId][r.candidate] / 2;
        r.state = State.Evaluating;
        emit Vetoed(cId, msg.sender, reason);
    }

    /// @notice Commit after an unvetoed window. Blocked while any challenge on the request is open.
    function commit(uint256 cId) external {
        Request storage r = requests[cId];
        require(r.state == State.CommitWindow && block.timestamp >= r.windowEnd, "window");
        require(r.openChallenges == 0, "open challenge");
        r.state = State.Committed;
        emit Committed(cId, r.candidate, r.altHashes[r.candidate]);
    }

    /// @notice After a request is finished, an agent's stake is released for withdrawal.
    function release(uint256 cId) external onlyAgent {
        Request storage r = requests[cId];
        require(r.state == State.Committed || r.state == State.Escalated, "unfinished");
        require(r.openChallenges == 0, "open challenge");
        Endorsement storage e = endorsementOf[cId][msg.sender];
        require(e.counted && !e.released, "nothing to release");
        e.released = true;
        agents[msg.sender].openRequests -= 1;
    }

    // ------------------------------------------------------------------ accountability
    /// @notice Dispute the score another agent endorsed. Requires a bond equal to `challengeBond`.
    function challenge(uint256 cId, address accused, uint16 claimedScore, bytes32 evidence)
        external payable onlyAgent returns (uint256 chId)
    {
        Request storage r = requests[cId];
        require(r.state == State.Evaluating || r.state == State.CommitWindow, "state");
        require(msg.value == challengeBond, "bond");
        require(accused != msg.sender && claimedScore <= 1000 && evidence != bytes32(0), "input");
        Endorsement storage e = endorsementOf[cId][accused];
        require(e.altPlus1 != 0 && !e.slashed, "no endorsement");
        uint8 role = uint8(agents[accused].role);
        require(((verifiableMask >> role) & uint8(1)) == uint8(1), "not verifiable");
        chId = ++challengeCount;
        challenges[chId] = Challenge(cId, msg.sender, accused, claimedScore, e.score, 0, CState.Open, msg.value, evidence);
        r.openChallenges += 1;
        emit Challenged(chId, cId, msg.sender, accused, claimedScore, evidence);
    }

    /// @notice The auditor re-runs the deterministic check and submits its score.
    function resolveChallenge(uint256 chId, uint16 auditedScore) external onlyAuditor {
        Challenge storage ch = challenges[chId];
        require(ch.state == CState.Open && auditedScore <= 1000, "challenge");
        Request storage r = requests[ch.cId];
        Endorsement storage e = endorsementOf[ch.cId][ch.accused];
        ch.auditedScore = auditedScore;
        r.openChallenges -= 1;

        uint16 s0 = ch.accusedScore;
        bool withinTolerance = (s0 > auditedScore ? s0 - auditedScore : auditedScore - s0) <= tolerance;
        if (withinTolerance || e.slashed) {
            // rejected (or already slashed by another challenge): the bond compensates the accused
            ch.state = CState.Rejected;
            credit[ch.accused] += ch.bond;
            emit ChallengeResolved(chId, false, auditedScore, 0);
            return;
        }

        ch.state = CState.Upheld;
        e.slashed = true;
        suspended[ch.cId][ch.accused] = true;
        // 1. remove the accused's current support (if it still endorses)
        uint8 alt = e.altPlus1 == 0 ? type(uint8).max : e.altPlus1 - 1;
        if (e.altPlus1 != 0) _withdraw(ch.cId, ch.accused, e);

        // 2. restore support the accused removed with stop signals
        for (uint8 t = 0; t < r.k; t++) {
            uint256 c = cutBy[ch.cId][ch.accused][t];
            if (c > 0) { support[ch.cId][t] += c; cutBy[ch.cId][ch.accused][t] = 0; }
        }

        // 3. slash: half to the challenger, half to the treasury; the bond is returned
        Agent storage a = agents[ch.accused];
        uint256 slashed = a.stake * slashBps / 10000;
        a.stake -= slashed;
        credit[ch.challenger] += ch.bond + slashed / 2;
        treasury += slashed - slashed / 2;

        // 4. a candidate that relied on fraudulent support returns to evaluation
        if (r.state == State.CommitWindow && r.candidate == alt) {
            r.state = State.Evaluating;
        }
        emit ChallengeResolved(chId, true, auditedScore, slashed);
    }

    // ------------------------------------------------------------------ views
    function getRequest(uint256 cId) external view returns (State, uint16, uint8, uint8, uint64, uint16) {
        Request storage r = requests[cId];
        return (r.state, r.round, r.k, r.candidate, r.windowEnd, r.openChallenges);
    }

    function roleMask(uint256 cId, uint8 alt) external view returns (uint8) { return _roleMask(cId, alt); }
}
