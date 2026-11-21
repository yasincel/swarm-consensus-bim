require("@nomicfoundation/hardhat-toolbox");

module.exports = {
  solidity: {
    version: "0.8.24",
    settings: { optimizer: { enabled: true, runs: 200 } }
  },
  networks: {
    hardhat: { accounts: { count: 60, accountsBalance: "10000000000000000000000" } }
  },
  gasReporter: {
    enabled: true,
    outputFile: "results/gas-report-v2.txt",
    noColors: true
  }
};
