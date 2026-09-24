// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

/// @title KorkoEvents
/// @notice Enregistre sur Avalanche C-Chain les evenements importants de KORKO :
///         debut/fin de session de surf, et points de gamification (les "tubes").
///
/// Simplifications assumees pour ce prototype :
/// - Un seul compte "operateur" (le backend cloud.py) est autorise a ecrire ; les usagers
///   ne signent rien et ne paient pas de gas. C'est un relais de confiance, pas encore de
///   la vraie abstraction de compte (ERC-4337). `riderId` (hash du numero de tel ou de
///   l'identifiant Privy) identifie un usager meme s'il n'a pas encore de wallet ; le jour
///   ou un vrai smart wallet lui est cree, `enregistrerWallet` fait le lien on-chain.
/// - Les montants (prix) sont en centimes d'euro (uint256) pour rester en entiers.
/// - Le contrat fait office de journal + score ; il ne detient ni ne transfere aucun fonds.
contract KorkoEvents {
    struct Session {
        bytes32 riderId;
        address riderWallet;   // address(0) si l'usager n'a pas (encore) de wallet
        string station;
        string balise;
        uint256 debut;         // block.timestamp au demarrage
        uint256 fin;           // block.timestamp a la fin (0 tant que non terminee)
        uint256 dureeSecondes;
        uint256 prixCentimes;
        bool terminee;
    }

    address public owner;
    address public operator;

    mapping(uint256 => Session) public sessions;
    mapping(bytes32 => uint256) public score;      // riderId -> total de points (tubes)
    mapping(bytes32 => address) public walletDe;   // riderId -> wallet enregistre

    event SessionDemarree(uint256 indexed sessionId, bytes32 indexed riderId, address indexed riderWallet, string station, string balise, uint256 t);
    event SessionTerminee(uint256 indexed sessionId, bytes32 indexed riderId, uint256 dureeSecondes, uint256 prixCentimes, uint256 t);
    event PointsAttribues(bytes32 indexed riderId, address indexed riderWallet, uint256 points, uint256 scoreTotal, string raison);
    event WalletEnregistre(bytes32 indexed riderId, address wallet);
    event OperateurChange(address ancien, address nouveau);

    modifier onlyOwner() {
        require(msg.sender == owner, "reserve au proprietaire");
        _;
    }

    modifier onlyOperator() {
        require(msg.sender == operator, "reserve a l'operateur");
        _;
    }

    constructor(address _operator) {
        owner = msg.sender;
        operator = _operator == address(0) ? msg.sender : _operator;
    }

    function changerOperateur(address nouveau) external onlyOwner {
        emit OperateurChange(operator, nouveau);
        operator = nouveau;
    }

    function demarrerSession(
        uint256 sessionId,
        bytes32 riderId,
        address riderWallet,
        string calldata station,
        string calldata balise
    ) external onlyOperator {
        require(sessions[sessionId].debut == 0, "session deja demarree");
        sessions[sessionId] = Session({
            riderId: riderId,
            riderWallet: riderWallet,
            station: station,
            balise: balise,
            debut: block.timestamp,
            fin: 0,
            dureeSecondes: 0,
            prixCentimes: 0,
            terminee: false
        });
        emit SessionDemarree(sessionId, riderId, riderWallet, station, balise, block.timestamp);
    }

    function terminerSession(
        uint256 sessionId,
        uint256 dureeSecondes,
        uint256 prixCentimes
    ) external onlyOperator {
        Session storage s = sessions[sessionId];
        require(s.debut != 0, "session inconnue");
        require(!s.terminee, "session deja terminee");
        s.fin = block.timestamp;
        s.dureeSecondes = dureeSecondes;
        s.prixCentimes = prixCentimes;
        s.terminee = true;
        emit SessionTerminee(sessionId, s.riderId, dureeSecondes, prixCentimes, block.timestamp);
    }

    function attribuerPoints(
        bytes32 riderId,
        address riderWallet,
        uint256 points,
        string calldata raison
    ) external onlyOperator {
        score[riderId] += points;
        emit PointsAttribues(riderId, riderWallet, points, score[riderId], raison);
    }

    /// @notice A appeler quand un wallet (embedded ou smart account) est cree pour un usager.
    function enregistrerWallet(bytes32 riderId, address wallet) external onlyOperator {
        walletDe[riderId] = wallet;
        emit WalletEnregistre(riderId, wallet);
    }
}
